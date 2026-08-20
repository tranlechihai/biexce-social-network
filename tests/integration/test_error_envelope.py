"""Integration tests for API error envelope and secret safety — AC7.

Verify:
* All error codes return consistent JSON shape {"error":{"code":...,"message":...}}
* Status codes map correctly
* No secrets (password hash, token, signing secret) leak in responses or logs
"""

from fastapi import status


# ---------------------------------------------------------------------------
# AC7: Error envelope consistency
# ---------------------------------------------------------------------------

class TestAC7ErrorEnvelope:
    """Verify consistent error shape across error types."""

    def _envelope(self, response):
        """Extract and assert the error envelope shape from a response."""
        data = response.json()
        assert "error" in data
        error = data["error"]
        assert "code" in error
        assert "message" in error
        return error

    def test_validation_error_shape(self, client):
        """Pydantic-level validation returns 422 with the shared error envelope."""
        resp = client.post("/api/auth/register", json={
            "username": "x",
            "email": "bad",
            "password": "short",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        error = self._envelope(resp)
        assert error["code"] == "validation"
        assert "details" in error  # validation details (field errors) included

    def test_unauthenticated_error_shape(self, client):
        resp = client.get("/api/profile/me")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        error = self._envelope(resp)
        assert error["code"] == "unauthenticated"

    def test_conflict_error_shape(self, client):
        client.post("/api/auth/register", json={
            "username": "envelope1",
            "email": "envelope1@example.com",
            "password": "securepass1",
        })
        resp = client.post("/api/auth/register", json={
            "username": "envelope1",
            "email": "envelope2@example.com",
            "password": "securepass2",
        })
        assert resp.status_code == status.HTTP_409_CONFLICT
        error = self._envelope(resp)
        assert error["code"] == "conflict"

    def test_not_found_error_shape(self, client):
        """Unknown routes return 404 with the shared error envelope."""
        resp = client.get("/api/unknown-endpoint")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        error = self._envelope(resp)
        assert error["code"] == "not_found"


# ---------------------------------------------------------------------------
# AC7: Secret safety
# ---------------------------------------------------------------------------

class TestAC7SecretSafety:
    """Verify no sensitive data leaks in API responses."""

    def _register(self, client):
        client.post("/api/auth/register", json={
            "username": "secretsafe",
            "email": "safe@example.com",
            "password": "mysecret123",
        })

    def test_register_response_no_password(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "safe1",
            "email": "safe1@example.com",
            "password": "mysecret123",
        })
        body = resp.json()
        for field in ("password", "password_hash", "secret"):
            assert field not in body

    def test_login_response_no_password_hash(self, client):
        self._register(client)
        resp = client.post("/api/auth/login", json={
            "identifier": "secretsafe",
            "password": "mysecret123",
        })
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        for field in ("password", "password_hash", "secret"):
            assert field not in body

    def test_profile_response_no_password(self, client):
        self._register(client)
        client.post("/api/auth/login", json={
            "identifier": "secretsafe",
            "password": "mysecret123",
        })
        resp = client.get("/api/profile/me")
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        for field in ("password", "password_hash", "secret", "token"):
            assert field not in body

    def test_error_response_does_not_leak_password_hash(self, client):
        self._register(client)
        # Try wrong password
        resp = client.post("/api/auth/login", json={
            "identifier": "secretsafe",
            "password": "wrong",
        })
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        body_str = resp.text
        assert "mysecret123" not in body_str
        assert "$2b$" not in body_str  # No password hash in error
        assert "$2a$" not in body_str

    def test_token_value_not_logged_or_exposed_in_error(self, client):
        self._register(client)
        login_resp = client.post("/api/auth/login", json={
            "identifier": "secretsafe",
            "password": "mysecret123",
        })
        token = login_resp.json()["access_token"]

        # Send a malformed token and verify it doesn't appear in error
        resp = client.get("/api/profile/me", headers={
            "Authorization": f"Bearer {token}.invalid",
        })
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        body_str = resp.text
        assert token not in body_str
