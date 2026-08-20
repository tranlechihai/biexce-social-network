"""Integration tests for T-021 — session & device lifecycle.

Covers: login issues a rotating refresh token (JSON + HttpOnly cookie);
refresh rotates the token every use and works with NO valid access JWT;
replay of an already-rotated token kills its session (401 refresh_replay);
the JWT-only legacy refresh path stays; ``GET /auth/sessions`` lists
devices with a ``current`` flag; ``DELETE /auth/sessions/{id}`` revokes one
device (own only); logout clears the refresh cookie; banned users cannot
refresh; register enforces the bcrypt 72-byte limit; web login sets the
refresh cookie.
"""

from datetime import datetime, timezone

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from ting_ting.main import app


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _tmp_db(client):
    """Swap the app engine to the per-test temp DB (conftest ``client``)."""
    yield


def _device() -> TestClient:
    """A browser-like client with its own cookie jar (no CSRF injection)."""
    return TestClient(app)


def _csrf_headers(c: TestClient) -> dict:
    tok = c.cookies.get("ting_ting_csrf")
    return {"X-CSRF-Token": tok} if tok else {}


def _mutate(c: TestClient, method: str, url: str, **kw):
    """POST/PUT/PATCH/DELETE with the double-submit CSRF header attached."""
    headers = dict(kw.pop("headers", None) or {})
    headers.update(_csrf_headers(c))
    if headers:
        kw["headers"] = headers
    return c.request(method, url, **kw)


_name_counter = 0


def register(c: TestClient, password: str = "securepass1") -> str:
    global _name_counter
    _name_counter += 1
    name = f"t21_{_name_counter}"
    resp = c.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t21.com", "password": password,
    })
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return name


def login(c: TestClient, name: str, password: str = "securepass1") -> dict:
    resp = c.post("/api/auth/login", json={"identifier": name, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------

class TestRefreshIssuance:
    def test_login_issues_refresh_token_and_cookie(self):
        c = _device()
        name = register(c)
        data = login(c, name)
        assert data["refresh_token"] and len(data["refresh_token"]) >= 32
        assert data["access_token"]
        assert c.cookies.get("ting_ting_refresh") == data["refresh_token"]

    def test_logout_clears_refresh_cookie(self):
        c = _device()
        name = register(c)
        data = login(c, name)
        rt = data["refresh_token"]
        resp = _mutate(c, "POST", "/api/auth/logout")
        assert resp.status_code == 200
        assert "ting_ting_refresh" not in c.cookies
        # The old token is dead through its (now revoked) session.
        resp = c.post("/api/auth/refresh", json={"refresh_token": rt})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "session_expired"

    def test_web_login_sets_refresh_cookie(self):
        c = _device()
        name = register(c)
        resp = _mutate(c, "POST", "/web/login", data={
            "identifier": name, "password": "securepass1",
        })
        assert resp.status_code < 400
        assert c.cookies.get("ting_ting_refresh")


# ---------------------------------------------------------------------------
# Rotation + replay
# ---------------------------------------------------------------------------

class TestRefreshRotation:
    def test_refresh_with_token_rotates_and_works_without_access_jwt(self):
        c = _device()
        name = register(c)
        data = login(c, name)
        rt = data["refresh_token"]

        # Fresh browser that lost its access cookie: refresh via JSON only.
        fresh = _device()
        resp = fresh.post("/api/auth/refresh", json={"refresh_token": rt})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["refresh_token"] and body["refresh_token"] != rt
        # The newly minted access token is immediately usable.
        me = fresh.get("/api/profile/me")
        assert me.status_code == 200, me.text

    def test_rotation_makes_old_token_replay(self):
        c = _device()
        name = register(c)
        data = login(c, name)
        rt_old = data["refresh_token"]

        resp = _mutate(c, "POST", "/api/auth/refresh", json={"refresh_token": rt_old})
        assert resp.status_code == 200, resp.text
        rt_new = resp.json()["refresh_token"]
        assert rt_new != rt_old
        assert c.cookies.get("ting_ting_refresh") == rt_new

        # Re-present the rotated token: replay -> session killed.
        resp = _mutate(c, "POST", "/api/auth/refresh", json={"refresh_token": rt_old})
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "refresh_replay"

        # Everything from that session is dead: fresh access token AND the
        # successor refresh token.
        me = c.get("/api/profile/me", headers=bearer(data["access_token"]))
        assert me.status_code == 401
        assert me.json()["error"]["code"] == "session_expired"
        resp = _mutate(c, "POST", "/api/auth/refresh", json={"refresh_token": rt_new})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "session_expired"

    def test_refresh_cookie_path_rotates(self):
        c = _device()
        name = register(c)
        login(c, name)
        rt_old = c.cookies["ting_ting_refresh"]

        resp = _mutate(c, "POST", "/api/auth/refresh", json={})
        assert resp.status_code == 200, resp.text
        rt_new = resp.json()["refresh_token"]
        assert rt_new and rt_new != rt_old

        # Old cookie value replayed from a fresh jar.
        fresh = _device()
        fresh.cookies.set("ting_ting_refresh", rt_old)
        resp = _mutate(fresh, "POST", "/api/auth/refresh", json={})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "refresh_replay"

    def test_invalid_refresh_token_rejected(self):
        c = _device()
        resp = c.post("/api/auth/refresh", json={"refresh_token": "not-a-real-token"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_refresh"

    def test_legacy_jwt_only_refresh_still_works(self):
        c = _device()
        name = register(c)
        data = login(c, name)

        # Bearer-only client (no cookies at all): pre-T-021 behavior.
        fresh = _device()
        resp = fresh.post("/api/auth/refresh", json={}, headers=bearer(data["access_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"]
        assert body["refresh_token"] is None

    def test_banned_user_cannot_refresh(self, tmp_session):
        from ting_ting.models import User
        from sqlalchemy import select

        c = _device()
        name = register(c)
        data = login(c, name)

        user = tmp_session.scalar(select(User).where(User.username == name))
        user.banned_at = datetime.now(timezone.utc)
        tmp_session.commit()

        resp = _mutate(c, "POST", "/api/auth/refresh", json={"refresh_token": data["refresh_token"]})
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "banned"
        me = c.get("/api/profile/me")
        assert me.status_code == 401

    def test_password_change_kills_other_devices_refresh_tokens(self):
        c1 = _device()
        name = register(c1)
        d1 = login(c1, name)
        c2 = _device()
        d2 = login(c2, name)

        resp = _mutate(c1, "POST", "/api/auth/change-password", json={
            "current_password": "securepass1", "new_password": "newpass123",
        })
        assert resp.status_code == 200, resp.text

        # Device 2's session was revoked -> its refresh token is dead too.
        resp = _mutate(c2, "POST", "/api/auth/refresh", json={"refresh_token": d2["refresh_token"]})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "session_expired"

        # Device 1 kept its session.
        resp = _mutate(c1, "POST", "/api/auth/refresh", json={"refresh_token": d1["refresh_token"]})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Device (session) management API
# ---------------------------------------------------------------------------

class TestSessionsApi:
    def test_list_sessions_flags_current(self):
        c1 = _device()
        name = register(c1)
        t1 = login(c1, name)
        c2 = _device()
        login(c2, name)

        resp = c1.get("/api/auth/sessions", headers=bearer(t1["access_token"]))
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert len(items) == 2
        currents = [i for i in items if i["current"]]
        assert len(currents) == 1
        other = [i for i in items if not i["current"]][0]
        assert other["id"] != currents[0]["id"]

    def test_unauthenticated_cannot_list_or_delete(self):
        c = _device()
        assert c.get("/api/auth/sessions").status_code == 401
        assert _mutate(c, "DELETE", "/api/auth/sessions/somesid").status_code == 401

    def test_delete_other_device_revokes_it_only(self):
        c1 = _device()
        name = register(c1)
        t1 = login(c1, name)
        c2 = _device()
        t2 = login(c2, name)

        items = c1.get("/api/auth/sessions", headers=bearer(t1["access_token"])).json()
        other = [i for i in items if not i["current"]][0]

        resp = _mutate(c1, "DELETE", f"/api/auth/sessions/{other['id']}")
        assert resp.status_code == 204, resp.text

        # Device 2 is dead, device 1 still alive.
        assert c2.get("/api/profile/me", headers=bearer(t2["access_token"])).status_code == 401
        assert c1.get("/api/profile/me").status_code == 200

        # Revoked session is gone: repeat delete -> 404.
        resp = _mutate(c1, "DELETE", f"/api/auth/sessions/{other['id']}")
        assert resp.status_code == 404

    def test_delete_own_session_kills_current(self):
        c = _device()
        name = register(c)
        login(c, name)
        items = c.get("/api/auth/sessions").json()
        current = [i for i in items if i["current"]][0]

        resp = _mutate(c, "DELETE", f"/api/auth/sessions/{current['id']}")
        assert resp.status_code == 204, resp.text
        assert "ting_ting_auth" not in c.cookies
        assert "ting_ting_refresh" not in c.cookies
        assert c.get("/api/profile/me").status_code == 401

    def test_cannot_delete_another_users_session(self):
        c1 = _device()
        name1 = register(c1)
        t1 = login(c1, name1)
        c2 = _device()
        name2 = register(c2)
        login(c2, name2)
        items2 = c2.get("/api/auth/sessions").json()
        target = items2[0]["id"]

        resp = _mutate(c1, "DELETE", f"/api/auth/sessions/{target}", headers=bearer(t1["access_token"]))
        assert resp.status_code == 404
        assert c2.get("/api/auth/sessions").status_code == 200


# ---------------------------------------------------------------------------
# Password length (bcrypt 72-byte limit at registration)
# ---------------------------------------------------------------------------

class TestRegisterPasswordBytes:
    def test_register_rejects_password_over_72_bytes(self):
        c = _device()
        long_pw = "é" * 50  # 100 bytes, 50 chars
        resp = c.post("/api/auth/register", json={
            "username": "t21_longpw", "email": "longpw@t21.com", "password": long_pw,
        })
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation"

    def test_register_accepts_72_bytes(self):
        c = _device()
        resp = c.post("/api/auth/register", json={
            "username": "t21_72", "email": "pw72@t21.com", "password": "a" * 72,
        })
        assert resp.status_code == 201, resp.text
