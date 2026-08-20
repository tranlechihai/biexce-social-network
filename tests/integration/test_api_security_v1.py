"""T-020 — canonical /api/v1 security parity.

Coverage:
* /api/v1 shares the exact same rate-limit quotas and limiter buckets as the
  deprecated /api alias (no quota evasion by version switching).
* Cookie-authenticated /api mutations require a valid CSRF token; Bearer
  requests are exempt.
* Unhandled 500s are logged with a correlation id (rid) and a generic
  envelope is returned to the client.
"""

import logging
import uuid

import pytest
from fastapi import FastAPI
from starlette import status
from starlette.requests import Request
from fastapi.testclient import TestClient

import ting_ting.config as config_mod
import ting_ting.main as main_mod
from ting_ting.config import Settings
from ting_ting.security import RateLimiter, request_rate_limit


def _fake_request(method: str, path: str) -> Request:
    return Request({
        "type": "http",
        "method": method,
        "path": path,
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [],
    })


def _enable_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on rate limiting with a fresh limiter for one test."""
    settings = Settings(
        jwt_secret="test-secret-do-not-use-in-production",
        rate_limit_enabled=True,
    )
    monkeypatch.setattr(config_mod, "_default_settings", settings)
    monkeypatch.setattr(main_mod, "rate_limiter", RateLimiter())


def _create_user(session, username: str, password: str):
    from ting_ting.auth import hash_password
    from ting_ting.models import User

    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Path identity: /api/v1 must be classified exactly like /api
# ---------------------------------------------------------------------------

class TestT020RateLimitIdentity:
    @pytest.mark.parametrize("path,expected", [
        ("POST /api/v1/auth/login", 20),
        ("POST /api/auth/login", 20),
        ("POST /api/v1/auth/register", 10),
        ("POST /api/auth/register", 10),
        ("POST /api/v1/auth/change-password", 10),
        ("POST /api/v1/posts", 120),
        ("POST /web/login", 20),
        ("GET /api/v1/notifications", None),
    ])
    def test_v1_and_legacy_share_quotas(self, path, expected):
        method, url = path.split(" ", 1)
        assert request_rate_limit(_fake_request(method, url)) == expected

    def test_unknown_web_path_not_limited(self):
        assert request_rate_limit(_fake_request("POST", "/static/style.css")) is None


# ---------------------------------------------------------------------------
# Real limiter: v1 paths are limited the same as legacy
# ---------------------------------------------------------------------------

class TestT020V1RateLimits:
    def test_v1_login_rate_limited_after_20_per_minute(self, client, monkeypatch):
        _enable_rate_limiting(monkeypatch)
        for _ in range(20):
            resp = client.post(
                "/api/v1/auth/login",
                json={"identifier": "nobody", "password": "wrong"},
            )
            assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": "nobody", "password": "wrong"},
        )
        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert resp.json()["error"]["code"] == "rate_limited"

    def test_v1_register_rate_limited_after_10_per_minute(self, client, monkeypatch):
        _enable_rate_limiting(monkeypatch)
        for i in range(10):
            ident = uuid.uuid4().hex[:8]
            resp = client.post(
                "/api/v1/auth/register",
                json={
                    "username": f"rl{i}_{ident}",
                    "email": f"rl{i}_{ident}@example.com",
                    "password": "correct123",
                },
            )
            assert resp.status_code == status.HTTP_201_CREATED
        ident = uuid.uuid4().hex[:8]
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "username": f"rl10_{ident}",
                "email": f"rl10_{ident}@example.com",
                "password": "correct123",
            },
        )
        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS


# ---------------------------------------------------------------------------
# Cookie-authenticated API mutations require CSRF
# ---------------------------------------------------------------------------

class _RawTestClient(TestClient):
    """Plain client (no conftest CSRF injection) — reproduces the cross-site
    attack shape: the browser cookie is sent, the X-CSRF-Token header is not."""


class TestT020CookieApiCsrf:
    def test_cookie_api_mutation_without_csrf_rejected(self, client, tmp_session):
        _create_user(tmp_session, "csrfuser", "correct123")
        login = client.post(
            "/web/login",
            data={"identifier": "csrfuser", "password": "correct123"},
        )
        assert login.status_code == status.HTTP_303_SEE_OTHER or login.status_code == status.HTTP_200_OK
        assert client.cookies.get("ting_ting_auth"), "web login must set the auth cookie"

        # Cross-site shape: same cookies, NO CSRF header at all -> 403.
        raw = _RawTestClient(main_mod.app)
        for name, value in client.cookies.items():
            raw.cookies.set(name, value)
        resp = raw.post(
            "/api/v1/posts", json={"content": "nope", "audience": "PUBLIC"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error"]["code"] == "forbidden"

        # ...with the double-submit token -> allowed.
        resp = client.post(
            "/api/v1/posts",
            json={"content": "ok", "audience": "PUBLIC"},
            headers={"X-CSRF-Token": client.cookies.get("ting_ting_csrf", "")},
        )
        assert resp.status_code == status.HTTP_201_CREATED

    def test_bearer_api_mutation_is_exempt_from_csrf(self, client, tmp_session):
        _create_user(tmp_session, "beareruser", "correct123")
        login = client.post(
            "/api/v1/auth/login",
            json={"identifier": "beareruser", "password": "correct123"},
        )
        token = login.json()["access_token"]

        # Fresh cookie-less client: Bearer only, no CSRF token.
        fresh = TestClient(main_mod.app)
        resp = fresh.post(
            "/api/v1/posts",
            json={"content": "api post", "audience": "PUBLIC"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# 500 logging
# ---------------------------------------------------------------------------

class TestT020ServerErrorLogging:
    def _boom_app(self) -> FastAPI:
        from ting_ting.errors import register_error_handlers

        app2 = FastAPI()
        register_error_handlers(app2)

        @app2.middleware("http")
        async def _rid(request, call_next):
            request.state.request_id = "rid-t020"
            return await call_next(request)

        @app2.get("/boom")
        def boom():
            raise RuntimeError("kaboom-t020")

        return app2

    def test_unhandled_exception_logged_with_rid(self, caplog):
        with caplog.at_level(logging.ERROR, logger="ting_ting.errors"):
            with TestClient(self._boom_app(), raise_server_exceptions=False) as c:
                resp = c.get("/boom", headers={"X-Request-ID": "ignored"})
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert resp.json()["error"]["code"] == "internal"
        messages = [r.getMessage() for r in caplog.records]
        assert any("rid=rid-t020" in m and "GET /boom" in m for m in messages)
        assert any("kaboom-t020" in m for m in messages)

    def test_client_sees_generic_message_only(self, caplog):
        with caplog.at_level(logging.ERROR, logger="ting_ting.errors"):
            with TestClient(self._boom_app(), raise_server_exceptions=False) as c:
                resp = c.get("/boom")
        body = resp.json()
        assert body["error"]["message"] == "An unexpected server error occurred."
        assert "kaboom" in str(caplog.records[-1].getMessage())
