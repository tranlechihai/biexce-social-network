"""Integration tests for T-023 — account lifecycle (export + deactivation).

Covers:
* GET /account/export — shape, own-data-only, auth required;
* POST /account/deactivate — password gate, session revocation, cookies,
  feed/search/profile/graph hiding for everyone but the owner, sign-in
  still possible;
* POST /account/reactivate — visibility restored;
* web surface: /web/account page + JSON download + web deactivate flow.
"""

from fastapi.testclient import TestClient


def _login(client: TestClient, name: str, password: str = "securepass1") -> None:
    client.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t23.com", "password": password,
    })
    resp = client.post("/api/auth/login", json={"identifier": name, "password": password})
    assert resp.status_code == 200, resp.text


def _post(client: TestClient, content: str = "hi") -> int:
    resp = _mutate(client, "POST", "/api/posts", json={"content": content, "audience": "PUBLIC"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestExport:
    def test_export_requires_auth(self, client, tmp_engine):
        resp = client.get("/api/account/export")
        assert resp.status_code == 401

    def test_export_shape_and_own_data_only(self, client, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_owner")
        pid = _post(c, "my post")

        other = TestClient(_app())
        _login(other, "t23_other")
        other_pid = _post(other, "their post")
        # other follows the owner -> follower id is visible data, fine.
        owner_id = c.get("/api/users/t23_owner").json()["id"]
        assert _mutate(other, "PUT", f"/api/social/follows/{owner_id}").status_code == 200

        resp = c.get("/api/account/export")
        assert resp.status_code == 200, resp.text
        doc = resp.json()

        assert doc["user"]["username"] == "t23_owner"
        assert doc["user"]["email"] == "t23_owner@t23.com"
        assert [p["id"] for p in doc["posts"]] == [pid]
        assert doc["posts"][0]["content"] == "my post"
        assert other_pid not in [p["id"] for p in doc["posts"]]
        # Only the owner's email ever appears in the document.
        assert "t23_other@t23.com" not in resp.text
        assert all(k in doc for k in (
            "user", "profile", "posts", "comments", "liked_post_ids",
            "saved_post_ids", "reposted_post_ids", "following_user_ids",
            "follower_user_ids", "notifications", "exported_at",
        ))

    def test_export_includes_comments_and_follows(self, client, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_exp2")
        pid = _post(c)
        cm = _mutate(c, "POST", f"/api/posts/{pid}/comments", json={"content": "mine"})
        assert cm.status_code == 201

        b = TestClient(_app())
        _login(b, "t23_exp2_fan")
        owner_id = c.get("/api/users/t23_exp2").json()["id"]
        assert _mutate(b, "PUT", f"/api/social/follows/{owner_id}").status_code == 200

        doc = c.get("/api/account/export").json()
        assert [x["content"] for x in doc["comments"]] == ["mine"]
        fan_id = b.get("/api/users/t23_exp2_fan").json()["id"]
        assert fan_id in doc["follower_user_ids"]
        assert doc["following_user_ids"] == []


class TestDeactivation:
    def test_deactivate_requires_correct_password(self, client, tmp_session, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_deact1")
        resp = _mutate(c, "POST", "/api/account/deactivate", json={"password": "wrongpass99"})
        assert resp.status_code == 403, resp.text
        assert _deactivated("t23_deact1", tmp_engine) is False

    def test_deactivate_hides_from_everyone_but_owner(self, client, tmp_session, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_deact2")
        pid = _post(c, "gone soon")
        fan = TestClient(_app())
        _login(fan, "t23_deact2_fan")
        owner_id = c.get("/api/users/t23_deact2").json()["id"]
        assert _mutate(fan, "PUT", f"/api/social/follows/{owner_id}").status_code == 200
        # fan sees the post before deactivation
        feed = fan.get("/api/feed").json()
        assert any(p["id"] == pid for p in feed)

        resp = _mutate(c, "POST", "/api/account/deactivate", json={"password": "securepass1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deactivated"] is True

        assert _deactivated("t23_deact2", tmp_engine) is True

        # Owner's own sessions were revoked (cookies + bearer dead).
        me = c.get("/api/profile/me")
        assert me.status_code == 401

        # Fan: post left the feed, profile redacted, graph 404, search hidden.
        feed = fan.get("/api/feed").json()
        assert all(p["id"] != pid for p in feed)
        prof = fan.get("/api/users/t23_deact2")
        assert prof.status_code == 200
        body = prof.json()
        assert body["display_name"] is None and body["bio"] is None
        assert body["follower_count"] is None
        assert fan.get("/api/users/t23_deact2/followers").status_code == 404
        search = fan.get("/api/users?q=t23_deact2").json()
        assert all(u["username"] != "t23_deact2" for u in search)

        # Owner can still view themselves (fresh sign-in — it is not blocked).
        c3 = TestClient(_app())
        assert c3.post("/api/auth/login",
                       json={"identifier": "t23_deact2", "password": "securepass1"}
                       ).status_code == 200
        assert c3.get("/api/users/t23_deact2").status_code == 200

    def test_signin_still_possible_and_reactivate_restores(self, client, tmp_session, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_deact3")
        pid = _post(c)
        fan = TestClient(_app())
        _login(fan, "t23_deact3_fan")
        owner_id = c.get("/api/users/t23_deact3").json()["id"]
        assert _mutate(fan, "PUT", f"/api/social/follows/{owner_id}").status_code == 200

        assert _mutate(c, "POST", "/api/account/deactivate",
                       json={"password": "securepass1"}).status_code == 200

        # Sign-in still works (deactivation is not a lockout).
        c2 = TestClient(_app())
        resp = c2.post("/api/auth/login",
                       json={"identifier": "t23_deact3", "password": "securepass1"})
        assert resp.status_code == 200, resp.text

        # Reactivate restores everything.
        resp = _mutate(c2, "POST", "/api/account/reactivate")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deactivated"] is False

        assert _deactivated("t23_deact3", tmp_engine) is False

        feed = fan.get("/api/feed").json()
        assert any(p["id"] == pid for p in feed)
        prof = fan.get("/api/users/t23_deact3").json()
        assert prof["follower_count"] is not None

    def test_deactivated_posts_hidden_from_direct_read(self, client, tmp_engine):
        """Like the feed, a direct post id must not bypass suppression:
        after deactivation the post 404s for third parties, while the owner
        (sign-in is not blocked) can still read their own post."""
        c = TestClient(_app())
        _login(c, "t23_deact5")
        pid = _post(c, "direct read check")
        fan = TestClient(_app())
        _login(fan, "t23_deact5_fan")
        assert fan.get(f"/api/posts/{pid}").status_code == 200

        resp = _mutate(c, "POST", "/api/account/deactivate", json={"password": "securepass1"})
        assert resp.status_code == 200, resp.text

        assert fan.get(f"/api/posts/{pid}").status_code == 404

        c2 = TestClient(_app())
        assert c2.post(
            "/api/auth/login",
            json={"identifier": "t23_deact5", "password": "securepass1"},
        ).status_code == 200
        assert c2.get(f"/api/posts/{pid}").status_code == 200


class TestWebAccount:
    def test_web_account_page_and_download(self, client, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_web")
        _post(c, "web post")

        page = c.get("/web/account")
        assert page.status_code == 200, page.text
        assert "Tài khoản" in page.text

        resp = c.get("/web/account/export")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/json")
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.json()["user"]["username"] == "t23_web"

    def test_web_deactivate_requires_password(self, client, tmp_session, tmp_engine):
        c = TestClient(_app(), follow_redirects=False)
        _login(c, "t23_web_deact")
        resp = _mutate(c, "POST", "/web/account/deactivate", data={"password": "nope-nope1"})
        assert resp.status_code == 200  # re-render with error
        assert "không đúng" in resp.text
        assert _deactivated("t23_web_deact", tmp_engine) is False

        resp = _mutate(c, "POST", "/web/account/deactivate", data={"password": "securepass1"})
        assert resp.status_code == 303
        assert _deactivated("t23_web_deact", tmp_engine) is True

    def test_web_profile_hidden_when_deactivated(self, client, tmp_session, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23_web_hid")
        _mutate(c, "POST", "/api/account/deactivate", json={"password": "securepass1"})

        fan = TestClient(_app())
        _login(fan, "t23_web_hid_fan")
        assert fan.get("/web/profile/t23_web_hid").status_code == 404
        # Owner re-logs in (sign-in is not blocked) and sees their profile.
        c2 = TestClient(_app())
        assert c2.post("/api/auth/login",
                       json={"identifier": "t23_web_hid", "password": "securepass1"}
                       ).status_code == 200
        assert c2.get("/web/profile/t23_web_hid").status_code == 200


def _deactivated(username: str, engine) -> bool:
    """Fresh-connection read — the test ORM session can hold a stale
    SQLite snapshot (same gotcha as the session-lifecycle tests)."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT deactivated_at IS NOT NULL FROM users WHERE username = :u"),
            {"u": username},
        ).one()
    return bool(row[0])


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
