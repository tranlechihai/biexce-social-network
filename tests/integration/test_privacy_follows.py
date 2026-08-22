"""T-024 — private accounts + follow approval.

Covers the locked design:

* follow of a private account creates a ``pending`` edge (a follow REQUEST,
  not a follow); only the owner may approve (``active`` + ``follow``
  notification to the requester) or reject (row deleted); only the requester
  may cancel while pending;
* blocks sever pending edges too;
* profile public toggle auto-approves all pending inbound requests;
* pending edges appear nowhere counts, lists, search, feeds or visibility
  matter — only ``active`` edges count as follows;
* visibility matrix: a private author's ``PUBLIC`` posts are visible to the
  author, friends and ACTIVE followers only; ``FOLLOWERS`` posts are visible
  to the author and active followers only; block always wins;
* web surface: pending follow state on the profile page, approve/reject
  buttons on the activity page.
"""

from fastapi.testclient import TestClient


def _app():
    from ting_ting.main import app

    return app


def _csrf_headers(c: TestClient) -> dict:
    tok = c.cookies.get("ting_ting_csrf")
    return {"X-CSRF-Token": tok} if tok else {}


def _mutate(c: TestClient, method: str, url: str, **kw):
    headers = dict(kw.pop("headers", None) or {})
    headers.update(_csrf_headers(c))
    if headers:
        kw["headers"] = headers
    return c.request(method, url, **kw)


def _user(c: TestClient, name: str) -> dict:
    """Register + log in a fresh client; return its public profile id."""
    resp = c.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t024.com", "password": "securepass1",
    })
    assert resp.status_code == 201, resp.text
    resp = c.post("/api/auth/login", json={"identifier": name, "password": "securepass1"})
    assert resp.status_code == 200, resp.text
    return {"username": name, "id": c.get(f"/api/users/{name}").json()["id"]}


def _post(c: TestClient, audience: str = "PUBLIC", content: str = "hello") -> int:
    resp = _mutate(c, "POST", "/api/posts", json={"content": content, "audience": audience})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_private(c: TestClient, is_private: bool) -> None:
    resp = _mutate(c, "PATCH", "/api/profile/me", json={"is_private": is_private})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_private"] is is_private


def _own_id(c: TestClient) -> int:
    return c.get("/api/profile/me").json()["id"]


def _follow(c: TestClient, target_id: int) -> dict:
    resp = _mutate(c, "PUT", f"/api/social/follows/{target_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _kinds(c: TestClient, kind: str) -> list[dict]:
    items = c.get("/api/notifications").json()["items"]
    return [i for i in items if i["kind"] == kind]


# ---------------------------------------------------------------------------
# Follow approval flow (API)
# ---------------------------------------------------------------------------

class TestFollowRequestFlow:

    def test_follow_private_target_creates_pending_request(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        ua, ub = _user(a, "t024_a1"), _user(b, "t024_b1")
        _set_private(b, True)

        body = _follow(a, ub["id"])
        assert body["state"] == "pending"
        assert body["active"] is False
        # The REQUESTER is not following yet — nothing counts or lists it.
        assert a.get("/api/users/t024_a1").json()["following_count"] == 0
        assert b.get("/api/users/t024_b1").json()["follower_count"] == 0

        # The owner gets a follow_request notification — not a "follow".
        reqs = _kinds(b, "follow_request")
        assert len(reqs) == 1 and reqs[0]["actor"]["id"] == ua["id"]
        assert _kinds(b, "follow") == []

    def test_follow_public_target_stays_immediate(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_a2"), _user(b, "t024_b2")

        body = _follow(a, ub["id"])
        assert body["state"] == "active"
        assert body["active"] is True
        assert _kinds(b, "follow") and _kinds(b, "follow_request") == []

    def test_inbox_and_outbox_lists(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        ua, ub = _user(a, "t024_a3"), _user(b, "t024_b3")
        _set_private(b, True)
        _follow(a, ub["id"])

        inbox = b.get("/api/social/follow-requests").json()  # default direction
        assert len(inbox) == 1
        assert inbox[0]["requester"]["id"] == ua["id"]
        assert inbox[0]["owner"]["id"] == ub["id"]
        assert inbox[0]["status"] == "pending"

        outgoing = a.get("/api/social/follow-requests?direction=outgoing").json()
        assert len(outgoing) == 1 and outgoing[0]["owner"]["id"] == ub["id"]
        assert a.get("/api/social/follow-requests?direction=inbox").json() == []

    def test_approve_marks_active_and_notifies_requester(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        ua, ub = _user(a, "t024_a4"), _user(b, "t024_b4")
        _set_private(b, True)
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]

        resp = _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

        assert _kinds(a, "follow") and _kinds(a, "follow_request") == []
        assert a.get("/api/users/t024_a4").json()["following_count"] == 1
        assert b.get("/api/users/t024_b4").json()["follower_count"] == 1
        followers = b.get("/api/users/t024_b4/followers").json()
        assert [f["id"] for f in followers] == [ua["id"]]

    def test_approve_by_stranger_or_requester_is_forbidden(self, client):
        a, b, c = TestClient(_app()), TestClient(_app()), TestClient(_app())
        _, ub, _ = _user(a, "t024_a5"), _user(b, "t024_b5"), _user(c, "t024_c5")
        _set_private(b, True)
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]

        # The requester cannot approve their own request.
        assert _mutate(a, "POST", f"/api/social/follow-requests/{req_id}/approve").status_code == 403
        # An unrelated user cannot approve it either.
        assert _mutate(c, "POST", f"/api/social/follow-requests/{req_id}/approve").status_code == 403

    def test_approve_unknown_request_404(self, client):
        b = TestClient(_app())
        _user(b, "t024_b6")
        assert _mutate(b, "POST", "/api/social/follow-requests/999999/approve").status_code == 404

    def test_approve_twice_converges_409(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_a7"), _user(b, "t024_b7")
        _set_private(b, True)
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        assert _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve").status_code == 200
        assert _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve").status_code == 409

    def test_reject_deletes_edge_and_refollow_pends(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_a8"), _user(b, "t024_b8")
        _set_private(b, True)
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]

        resp = _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/reject")
        assert resp.status_code == 204, resp.text
        assert b.get("/api/social/follow-requests").json() == []
        assert a.get("/api/social/follow-requests?direction=outgoing").json() == []

        # Re-following a still-private account pends it again.
        assert _follow(a, ub["id"])["state"] == "pending"
        assert len(b.get("/api/social/follow-requests").json()) == 1

        # Rejected requesters learn nothing through a second reject.
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/reject")
        # (idempotent convergence — the row is gone; 404 or 204 both prove
        # no edge resurrected)
        assert b.get("/api/users/t024_b8").json()["follower_count"] == 0

    def test_reject_by_stranger_is_forbidden(self, client):
        a, b, c = TestClient(_app()), TestClient(_app()), TestClient(_app())
        _, ub, _ = _user(a, "t024_a9"), _user(b, "t024_b9"), _user(c, "t024_c9")
        _set_private(b, True)
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        assert _mutate(c, "POST", f"/api/social/follow-requests/{req_id}/reject").status_code == 403

    def test_requester_cancels_pending_edge(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_a10"), _user(b, "t024_b10")
        _set_private(b, True)
        _follow(a, ub["id"])
        assert len(b.get("/api/social/follow-requests").json()) == 1

        resp = _mutate(a, "DELETE", f"/api/social/follows/{ub['id']}")
        assert resp.status_code == 200, resp.text
        assert b.get("/api/social/follow-requests").json() == []
        assert a.get("/api/social/follow-requests?direction=outgoing").json() == []

    def test_block_severs_pending_edges(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_a11"), _user(b, "t024_b11")
        _set_private(b, True)
        _follow(a, ub["id"])
        assert len(b.get("/api/social/follow-requests").json()) == 1

        resp = _mutate(b, "POST", "/api/social/blocks", json={"target_user_id": _own_id(a)})
        assert resp.status_code == 201, resp.text
        assert b.get("/api/social/follow-requests").json() == []
        assert a.get("/api/social/follow-requests?direction=outgoing").json() == []


# ---------------------------------------------------------------------------
# Privacy toggle + visibility matrix
# ---------------------------------------------------------------------------

class TestPrivacyVisibility:

    def _private_author(self) -> tuple[TestClient, TestClient, dict]:
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_av1"), _user(b, "t024_bv1")
        _set_private(b, True)
        return a, b, ub

    def test_private_public_post_visibility_matrix(self, client):
        a, b, ub = self._private_author()
        pid = _post(b, "PUBLIC")

        assert b.get(f"/api/posts/{pid}").status_code == 200  # author

        _follow(a, ub["id"])  # pending
        assert a.get(f"/api/posts/{pid}").status_code == 404  # pending: nothing

        stranger = TestClient(_app())
        _user(stranger, "t024_sv1")
        assert stranger.get(f"/api/posts/{pid}").status_code == 404  # stranger

        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert a.get(f"/api/posts/{pid}").status_code == 200  # active follower

    def test_private_public_post_visible_to_accepted_friends(self, client):
        a, b, ub = self._private_author()
        pid = _post(b, "PUBLIC")

        # A friend (no follow at all) still sees a private account's posts —
        # a friendship is mutual opt-in.
        resp = _mutate(a, "POST", "/api/social/requests", json={"target_user_id": ub["id"]})
        assert resp.status_code == 201, resp.text
        req_id = resp.json()["id"]
        _mutate(b, "POST", "/api/social/requests/accept", json={"request_id": req_id})

        assert a.get(f"/api/posts/{pid}").status_code == 200

    def test_followers_audience_gates_on_active_follow(self, client):
        a, b, ub = self._private_author()
        pid = _post(b, "FOLLOWERS")

        assert b.get(f"/api/posts/{pid}").status_code == 200  # author

        _follow(a, ub["id"])  # pending — sees nothing, even for FOLLOWERS
        assert a.get(f"/api/posts/{pid}").status_code == 404

        stranger = TestClient(_app())
        _user(stranger, "t024_sf1")
        assert stranger.get(f"/api/posts/{pid}").status_code == 404

        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert a.get(f"/api/posts/{pid}").status_code == 200

    def test_public_author_followers_feed_only(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_af1"), _user(b, "t024_bf1")
        _follow(a, ub["id"])  # public author → active immediately
        assert a.get(f"/api/users/{ub['username']}").json()["is_private"] is False

        pid = _post(b, "FOLLOWERS")
        assert a.get(f"/api/posts/{pid}").status_code == 200
        stranger = TestClient(_app())
        _user(stranger, "t024_sf2")
        assert stranger.get(f"/api/posts/{pid}").status_code == 404

    def test_block_wins_over_active_follow(self, client):
        a, b, ub = self._private_author()
        pid = _post(b, "PUBLIC")
        _follow(a, ub["id"])
        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert a.get(f"/api/posts/{pid}").status_code == 200

        a_id = a.get("/api/profile/me").json()["id"]
        assert _mutate(b, "POST", "/api/social/blocks", json={"target_user_id": a_id}).status_code == 201
        assert a.get(f"/api/posts/{pid}").status_code == 404  # block always wins

    def test_going_public_auto_approves_pending(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        c = TestClient(_app())
        _, ub, _ = _user(a, "t024_ap1"), _user(b, "t024_bp1"), _user(c, "t024_cp1")
        _set_private(b, True)
        pid = _post(b, "PUBLIC")
        _follow(a, ub["id"])
        _follow(c, ub["id"])
        assert a.get(f"/api/posts/{pid}").status_code == 404
        assert c.get(f"/api/posts/{pid}").status_code == 404

        _set_private(b, False)  # public — everyone who asked comes in

        assert _kinds(a, "follow") and _kinds(c, "follow")
        assert a.get(f"/api/posts/{pid}").status_code == 200
        assert c.get(f"/api/posts/{pid}").status_code == 200
        assert b.get("/api/users/t024_bp1").json()["follower_count"] == 2
        # No inbox left.
        assert b.get("/api/social/follow-requests").json() == []


# ---------------------------------------------------------------------------
# Feed-level (SQL) privacy
# ---------------------------------------------------------------------------

class TestPrivacyFeeds:

    def test_for_you_feed_hides_private_author_from_non_followers(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_fa1"), _user(b, "t024_fb1")
        _set_private(b, True)
        pid = _post(b, "PUBLIC")
        stranger = TestClient(_app())
        _user(stranger, "t024_fs1")

        visible = [p["id"] for p in stranger.get("/api/feed").json()]
        assert pid not in visible  # stranger: hidden

        _follow(a, ub["id"])  # pending: still hidden
        assert pid not in [p["id"] for p in a.get("/api/feed").json()]

        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert pid in [p["id"] for p in a.get("/api/feed").json()]  # active: visible

    def test_following_feed_ignores_pending(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_fa2"), _user(b, "t024_fb2")
        _set_private(b, True)
        pid = _post(b, "PUBLIC")
        feed = [p["id"] for p in a.get("/api/feed/following").json()]
        assert pid not in feed  # not following yet

        _follow(a, ub["id"])  # pending
        assert pid not in [p["id"] for p in a.get("/api/feed/following").json()]

        req_id = b.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(b, "POST", f"/api/social/follow-requests/{req_id}/approve")
        assert pid in [p["id"] for p in a.get("/api/feed/following").json()]

    def test_followers_audience_reaches_active_followers_in_feed(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _, ub = _user(a, "t024_fa3"), _user(b, "t024_fb3")
        _follow(a, ub["id"])  # public → active
        stranger = TestClient(_app())
        _user(stranger, "t024_fs3")

        pid = _post(b, "FOLLOWERS")
        assert pid in [p["id"] for p in a.get("/api/feed/following").json()]
        assert pid not in [p["id"] for p in stranger.get("/api/feed").json()]


# ---------------------------------------------------------------------------
# Profile/privacy API surface
# ---------------------------------------------------------------------------

class TestPrivacyProfileApi:

    def test_is_private_in_me_and_public_responses(self, client):
        b = TestClient(_app())
        meta = _user(b, "t024_pm1")
        assert b.get("/api/profile/me").json()["is_private"] is False

        _set_private(b, True)
        pub = b.get("/api/users/t024_pm1").json()
        assert pub["is_private"] is True
        assert meta["id"] == pub["id"]

    def test_patch_is_private_false_roundtrip(self, client):
        b = TestClient(_app())
        _user(b, "t024_pm2")
        _set_private(b, True)
        _set_private(b, False)
        assert b.get("/api/users/t024_pm2").json()["is_private"] is False


# ---------------------------------------------------------------------------
# Web surface
# ---------------------------------------------------------------------------

class TestWebPrivacy:

    def test_web_profile_shows_request_state_and_private_badge(self, client):
        a, b, stranger = TestClient(_app()), TestClient(_app()), TestClient(_app())
        _, _, _ = _user(a, "t024_wa1"), _user(b, "t024_wb1"), _user(stranger, "t024_ws1")
        _set_private(b, True)

        page = stranger.get("/web/profile/t024_wb1")
        assert page.status_code == 200
        assert "private-badge" in page.text  # stranger sees the badge

        resp = _mutate(a, "POST", "/web/social/follow", data={"target_username": "t024_wb1"})
        assert resp.status_code in (200, 303, 302), resp.text
        page = a.get("/web/profile/t024_wb1")
        assert "Đã yêu cầu" in page.text  # pending state button

        req = b.get("/api/social/follow-requests").json()
        assert len(req) == 1
        _mutate(b, "POST", f"/api/social/follow-requests/{req[0]['id']}/approve")
        page = a.get("/web/profile/t024_wb1")
        assert "Đang theo dõi" in page.text

    def test_web_activity_page_approve_and_reject(self, client):
        a, b, c = TestClient(_app()), TestClient(_app()), TestClient(_app())
        _, ub, _ = _user(a, "t024_wa2"), _user(b, "t024_wb2"), _user(c, "t024_wc2")
        _set_private(b, True)
        _follow(a, ub["id"])
        _follow(c, ub["id"])

        # The owner's activity page lists BOTH requests with action buttons.
        page = b.get("/web/activity")
        assert page.status_code == 200
        assert "follow_request" in page.text
        assert page.text.count("/web/social/follow-requests/") >= 2

        inbox = b.get("/api/social/follow-requests").json()
        by_actor = {r["requester"]["id"]: r["id"] for r in inbox}
        a_id = a.get("/api/profile/me").json()["id"]
        c_id = c.get("/api/profile/me").json()["id"]

        # Approve a, reject c — through the web forms.
        _mutate(b, "POST", f"/web/social/follow-requests/{by_actor[a_id]}/approve")
        _mutate(b, "POST", f"/web/social/follow-requests/{by_actor[c_id]}/reject")
        assert b.get("/api/social/follow-requests").json() == []
        assert a.get("/api/users/t024_wa2").json()["following_count"] == 1
        assert c.get("/api/users/t024_wc2").json()["following_count"] == 0
        assert _kinds(a, "follow") and _kinds(c, "follow") == []
