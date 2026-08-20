"""Integration tests for authentication acceptance criteria — AC1, AC2, AC3, AC4, AC5."""

import pytest
from fastapi import status
from sqlalchemy import text

from ting_ting.auth import hash_password

# ---------------------------------------------------------------------------
# AC1: Registration
# ---------------------------------------------------------------------------

class TestAC1Register:
    @pytest.fixture
    def valid_payload(self):
        return {
            "username": "newuser1",
            "email": "new@example.com",
            "password": "securepass1",
        }

    def test_register_creates_user(self, client, valid_payload, tmp_engine):
        resp = client.post("/api/auth/register", json=valid_payload)
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["username"] == "newuser1"
        assert data["email"] == "new@example.com"
        assert "password_hash" not in data
        assert "access_token" not in data

        # Verify persisted hash is bcrypt, not plain text
        with tmp_engine.connect() as conn:
            row = conn.execute(
                text("SELECT password_hash FROM users WHERE username = 'newuser1'")
            ).fetchone()
        assert row is not None
        pw_hash = row[0]
        assert pw_hash != "securepass1"
        assert pw_hash.startswith("$2"), f"Expected bcrypt, got {pw_hash[:5]}"

    def test_register_returns_non_sensitive_representation(self, client, valid_payload):
        resp = client.post("/api/auth/register", json=valid_payload)
        data = resp.json()
        for sensitive in ("password_hash", "password", "token", "secret"):
            assert sensitive not in data


# ---------------------------------------------------------------------------
# AC2: Login
# ---------------------------------------------------------------------------

class TestAC2Login:
    def _setup_user(self, tmp_session):
        user_data = {
            "username": "loginuser",
            "email": "login@example.com",
            "password": "correct123",
        }
        tmp_session.request_context = {**user_data, "password_hash": hash_password(user_data["password"])}
        from ting_ting.models import User
        u = User(
            username=user_data["username"],
            email=user_data["email"],
            password_hash=hash_password(user_data["password"]),
        )
        tmp_session.add(u)
        tmp_session.commit()
        tmp_session.refresh(u)
        return user_data

    def test_login_by_username(self, client, tmp_session):
        self._setup_user(tmp_session)
        resp = client.post("/api/auth/login", json={
            "identifier": "loginuser",
            "password": "correct123",
        })
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_by_email(self, client, tmp_session):
        self._setup_user(tmp_session)
        resp = client.post("/api/auth/login", json={
            "identifier": "login@example.com",
            "password": "correct123",
        })
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert "access_token" in data

    def test_login_cookie_set(self, client, tmp_session):
        self._setup_user(tmp_session)
        resp = client.post("/api/auth/login", json={
            "identifier": "loginuser",
            "password": "correct123",
        })
        # Cookie should be present; check Set-Cookie header
        set_cookie_header = resp.headers.get("set-cookie", "")
        assert "ting_ting_auth=" in set_cookie_header
        assert "HttpOnly" in set_cookie_header
        assert "samesite=lax" in set_cookie_header.lower()

    def test_login_invalid_credentials_returns_401(self, client):
        resp = client.post("/api/auth/login", json={
            "identifier": "nonexistent",
            "password": "wrongpass",
        })
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        data = resp.json()
        assert data["error"]["code"] == "unauthenticated"
        # Does not identify which field was wrong
        msg = data["error"]["message"].lower()
        assert "username" not in msg
        assert "email" not in msg


# ---------------------------------------------------------------------------
# AC3: Validation and conflict on registration
# ---------------------------------------------------------------------------

class TestAC3Validation:
    def _register_first(self, client):
        """Register a user to set up conflict conditions."""
        client.post("/api/auth/register", json={
            "username": "existing1",
            "email": "existing1@example.com",
            "password": "securepass1",
        })

    def test_duplicate_username_conflict(self, client):
        self._register_first(client)
        resp = client.post("/api/auth/register", json={
            "username": "existing1",
            "email": "different@example.com",
            "password": "securepass2",
        })
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_duplicate_email_conflict(self, client):
        self._register_first(client)
        resp = client.post("/api/auth/register", json={
            "username": "different1",
            "email": "existing1@example.com",
            "password": "securepass2",
        })
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_duplicate_normalized_username(self, client):
        client.post("/api/auth/register", json={
            "username": "CaseUser",
            "email": "case@example.com",
            "password": "securepass1",
        })
        resp = client.post("/api/auth/register", json={
            "username": "caseuser",
            "email": "other@example.com",
            "password": "securepass2",
        })
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_duplicate_normalized_email(self, client):
        client.post("/api/auth/register", json={
            "username": "emailuser1",
            "email": "Email@Example.Com",
            "password": "securepass1",
        })
        resp = client.post("/api/auth/register", json={
            "username": "emailuser2",
            "email": "email@example.com",
            "password": "securepass2",
        })
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_missing_password_validation(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "validuser",
            "email": "valid@example.com",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_missing_username_validation(self, client):
        resp = client.post("/api/auth/register", json={
            "email": "valid@example.com",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_short_password_rejected(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "shortpw",
            "email": "shortpw@example.com",
            "password": "abc",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_short_username_rejected(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "ab",
            "email": "shortu@example.com",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_malformed_email_rejected(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "emailtest",
            "email": "not-an-email",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_no_duplicate_row_on_conflict(self, client, tmp_engine):
        """Prove that a conflict response does NOT create a second row."""
        client.post("/api/auth/register", json={
            "username": "unique1",
            "email": "unique1@example.com",
            "password": "securepass1",
        })
        client.post("/api/auth/register", json={
            "username": "unique1",
            "email": "unique1@example.com",
            "password": "securepass2",
        })
        with tmp_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE username = 'unique1'")
            ).scalar()
        assert count == 1


# ---------------------------------------------------------------------------
# AC4: Auth transports — anonymous, bearer, cookie
# ---------------------------------------------------------------------------

class TestAC4AuthTransports:
    def test_anonymous_get_profile_401(self, client):
        resp = client.get("/api/profile/me")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_malformed_token_401(self, client):
        resp = client.get("/api/profile/me", headers={
            "Authorization": "Bearer not.a.valid.token",
        })
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_valid_bearer_identifies_user(self, client, tmp_session):
        from ting_ting.models import User
        user = User(
            username="bearertest",
            email="bearer@test.com",
            password_hash=hash_password("pass1234"),
        )
        tmp_session.add(user)
        tmp_session.commit()
        tmp_session.refresh(user)

        # Login to get token
        login_resp = client.post("/api/auth/login", json={
            "identifier": "bearertest",
            "password": "pass1234",
        })
        token = login_resp.json()["access_token"]

        # Use Bearer header
        profile_resp = client.get("/api/profile/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert profile_resp.status_code == status.HTTP_200_OK
        assert profile_resp.json()["username"] == "bearertest"

    def test_valid_cookie_identifies_same_user(self, client, tmp_session):
        from ting_ting.models import User
        user = User(
            username="cookietest",
            email="cookie@test.com",
            password_hash=hash_password("pass1234"),
        )
        tmp_session.add(user)
        tmp_session.commit()
        tmp_session.refresh(user)

        # Login to get cookie
        client.post("/api/auth/login", json={
            "identifier": "cookietest",
            "password": "pass1234",
        })

        # Cookie is already set by TestClient
        profile_resp = client.get("/api/profile/me")
        assert profile_resp.status_code == status.HTTP_200_OK
        assert profile_resp.json()["username"] == "cookietest"
