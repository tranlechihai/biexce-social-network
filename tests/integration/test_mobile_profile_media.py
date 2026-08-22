"""T-025 — mobile profile/media API.

Covers:

* ``POST /api/profile/avatar`` — owner-only avatar replacement: signature
  validation (images only), dangerous-content scan, size cap, quota errors
  mapped to the stable envelope, old file cleanup, unauthenticated 401;
* ``GET /api/users/{username}/posts`` — keyset-paginated profile posts
  applying the T-024 privacy matrix per (author, viewer) pair: unknown /
  banned / deactivated author and blocked pairs 404, the owner sees
  everything, a private author's PUBLIC posts go to friends and ACTIVE
  followers only (pending sees nothing), FOLLOWERS to active followers,
  FRIENDS to accepted friends, ONLY_ME to nobody else.
"""

from pathlib import Path

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
    resp = c.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t025.com", "password": "securepass1",
    })
    assert resp.status_code == 201, resp.text
    resp = c.post("/api/auth/login", json={"identifier": name, "password": "securepass1"})
    assert resp.status_code == 200, resp.text
    return {"username": name, "id": c.get(f"/api/users/{name}").json()["id"]}


def _post(c: TestClient, audience: str = "PUBLIC", content: str = "p") -> int:
    resp = _mutate(c, "POST", "/api/posts", json={"content": content, "audience": audience})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Avatar upload API
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TestAvatarApi:

    def test_upload_replaces_avatar_and_persists(self, client):
        c = TestClient(_app())
        meta = _user(c, "t025_av1")

        resp = _mutate(c, "POST", "/api/profile/avatar",
                       files={"avatar_file": ("me.png", _PNG, "image/png")})
        assert resp.status_code == 200, resp.text
        url = resp.json()["avatar_url"]
        assert url.startswith("/media/avatar-")

        from ting_ting.media import UPLOADS_DIR
        assert (UPLOADS_DIR / Path(url).name).is_file()
        # The public profile serves the new URL.
        assert c.get("/api/users/t025_av1").json()["avatar_url"] == url
        del meta

    def test_upload_removes_previous_file(self, client):
        c = TestClient(_app())
        _user(c, "t025_av2")
        from ting_ting.media import UPLOADS_DIR
        first = _mutate(c, "POST", "/api/profile/avatar",
                        files={"avatar_file": ("one.png", _PNG, "image/png")})
        assert first.status_code == 200, first.text
        old_name = Path(first.json()["avatar_url"]).name

        second = _mutate(c, "POST", "/api/profile/avatar",
                         files={"avatar_file": ("two.png", _PNG, "image/png")})
        assert second.status_code == 200, second.text
        new_name = Path(second.json()["avatar_url"]).name
        assert new_name != old_name
        assert not (UPLOADS_DIR / old_name).exists()
        assert (UPLOADS_DIR / new_name).is_file()

    def test_rejects_non_image_bytes(self, client):
        c = TestClient(_app())
        _user(c, "t025_av3")
        resp = _mutate(c, "POST", "/api/profile/avatar",
                       files={"avatar_file": ("x.bin", b"plain text bytes", "application/octet-stream")})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_media"

    def test_rejects_video_container(self, client):
        c = TestClient(_app())
        _user(c, "t025_av4")
        mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
        resp = _mutate(c, "POST", "/api/profile/avatar",
                       files={"avatar_file": ("clip.mp4", mp4, "video/mp4")})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_media"

    def test_rejects_dangerous_payload_behind_image_header(self, client):
        c = TestClient(_app())
        _user(c, "t025_av5")
        sneaky = _PNG + b"PK\x03\x04"  # zip marker appended after a PNG header
        resp = _mutate(c, "POST", "/api/profile/avatar",
                       files={"avatar_file": ("poc.png", sneaky, "image/png")})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "blocked_content"

    def test_rejects_oversized_image(self, client):
        c = TestClient(_app())
        _user(c, "t025_av6")
        from ting_ting.uploads import AVATAR_MAX
        huge = _PNG + b"\x00" * AVATAR_MAX
        resp = _mutate(c, "POST", "/api/profile/avatar",
                       files={"avatar_file": ("big.png", huge, "image/png")})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "media_too_large"

    def test_requires_auth(self, client):
        c = TestClient(_app())
        resp = c.post("/api/profile/avatar",
                      files={"avatar_file": ("anon.png", _PNG, "image/png")})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Profile posts cursor
# ---------------------------------------------------------------------------

class TestProfilePosts:

    def test_own_profile_paged_keyset(self, client):
        c = TestClient(_app())
        _user(c, "t025_pp1")
        # Six posts of every audience, oldest first.
        ids = [_post(c, "PUBLIC"), _post(c, "ONLY_ME"), _post(c, "FRIENDS"),
               _post(c, "PUBLIC"), _post(c, "FOLLOWERS"), _post(c, "PUBLIC")]

        r1 = c.get("/api/users/t025_pp1/posts", params={"limit": 4})
        assert r1.status_code == 200
        # Owner sees every audience, newest first, bounded by limit.
        assert [p["id"] for p in r1.json()] == list(reversed(ids[-4:]))
        assert r1.headers["X-Next-Cursor"], "expected a next cursor"

        r2 = c.get("/api/users/t025_pp1/posts",
                   params={"limit": 4, "cursor": r1.headers["X-Next-Cursor"]})
        assert r2.headers.get("X-Next-Cursor") is None
        seen = [p["id"] for p in r1.json()] + [p["id"] for p in r2.json()]
        # No duplicates, no gaps, stable order across pages.
        assert seen == list(reversed(ids))

    def test_stranger_sees_only_public_of_public_author(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _user(a, "t025_pa1")
        _user(b, "t025_pb1")
        pid_public_1 = _post(b, "PUBLIC")
        pid_public_2 = _post(b, "PUBLIC")
        pid_friends = _post(b, "FRIENDS")
        pid_only_me = _post(b, "ONLY_ME")
        pid_followers = _post(b, "FOLLOWERS")

        items = a.get("/api/users/t025_pb1/posts").json()
        assert [p["id"] for p in items] == [pid_public_2, pid_public_1]
        for hidden in (pid_friends, pid_only_me, pid_followers):
            assert hidden not in [p["id"] for p in items]

    def test_private_author_follower_friend_pending_stranger(self, client):
        author = TestClient(_app())
        ua = _user(author, "t025_pca")
        # Make the author private, then create one post per audience.
        _mutate(author, "PATCH", "/api/profile/me", json={"is_private": True})
        pid_public = _post(author, "PUBLIC")
        pid_friend = _post(author, "FRIENDS")
        pid_follower = _post(author, "FOLLOWERS")
        _post(author, "ONLY_ME")

        follower, friend, pending, stranger = (TestClient(_app()) for _ in range(4))
        _user(follower, "t025_pcf")
        _user(friend, "t025_pcfr")
        _user(pending, "t025_pcp")
        _user(stranger, "t025_pcs")

        # Active follower: PUBLIC + FOLLOWERS.
        _mutate(follower, "PUT", f"/api/social/follows/{ua['id']}")
        req = author.get("/api/social/follow-requests").json()
        _mutate(author, "POST", f"/api/social/follow-requests/{req[0]['id']}/approve")
        items = follower.get("/api/users/t025_pca/posts").json()
        assert [p["id"] for p in items] == [pid_follower, pid_public]

        # Accepted friend (no follow): PUBLIC + FRIENDS.
        _mutate(friend, "POST", "/api/social/requests", json={"target_user_id": ua["id"]})
        req = author.get("/api/social/requests")
        req_id = [r for r in req.json() if r["state"] == "pending"][0]["id"]
        _mutate(author, "POST", "/api/social/requests/accept", json={"request_id": req_id})
        items = friend.get("/api/users/t025_pca/posts").json()
        assert [p["id"] for p in items] == [pid_friend, pid_public]

        # Pending follow request: sees NOTHING (not even PUBLIC).
        _mutate(pending, "PUT", f"/api/social/follows/{ua['id']}")
        assert pending.get("/api/users/t025_pca/posts").json() == []

        # Stranger: sees nothing.
        assert stranger.get("/api/users/t025_pca/posts").json() == []

    def test_blocked_pair_gets_404(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        ua, _ = _user(a, "t025_pba"), _user(b, "t025_pbb")
        _post(b, "PUBLIC")
        assert a.get("/api/users/t025_pbb/posts").status_code == 200

        assert _mutate(b, "POST", "/api/social/blocks", json={"target_user_id": ua["id"]}).status_code == 201
        assert a.get("/api/users/t025_pbb/posts").status_code == 404

    def test_deactivated_author_gets_404(self, client):
        a, b = TestClient(_app()), TestClient(_app())
        _user(a, "t025_pda")
        _user(b, "t025_pdb")
        _post(b, "PUBLIC")
        assert a.get("/api/users/t025_pdb/posts").status_code == 200

        resp = _mutate(b, "POST", "/api/account/deactivate", json={"password": "securepass1"})
        assert resp.status_code == 200, resp.text
        assert a.get("/api/users/t025_pdb/posts").status_code == 404

    def test_unknown_username_404(self, client):
        c = TestClient(_app())
        _user(c, "t025_pdk")
        assert c.get("/api/users/ghost_no_such_user/posts").status_code == 404

    def test_malformed_cursor_restarts_not_500(self, client):
        c = TestClient(_app())
        _user(c, "t025_pdm")
        _post(c, "PUBLIC")
        resp = c.get("/api/users/t025_pdm/posts", params={"cursor": "not-a-cursor!!"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1
