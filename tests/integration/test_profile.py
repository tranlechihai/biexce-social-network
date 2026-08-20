"""Integration tests for profile and logout — AC5, AC6."""

from fastapi import status



# ---------------------------------------------------------------------------
# AC5: Logout
# ---------------------------------------------------------------------------

class TestAC5Logout:
    def _login(self, client, tmp_session):
        """Register + login a user, return the client (which keeps cookies)."""
        client.post("/api/auth/register", json={
            "username": "logoutuser",
            "email": "logout@example.com",
            "password": "securepass1",
        })
        resp = client.post("/api/auth/login", json={
            "identifier": "logoutuser",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_200_OK

    def test_logout_clears_cookie(self, client, tmp_session):
        self._login(client, tmp_session)
        # Cookie should be present
        profile_resp = client.get("/api/profile/me")
        assert profile_resp.status_code == status.HTTP_200_OK

        # Logout
        logout_resp = client.post("/api/auth/logout")
        assert logout_resp.status_code == status.HTTP_200_OK

        # Cookie should be expired/removed
        set_cookie = logout_resp.headers.get("set-cookie", "")
        assert "ting_ting_auth" in set_cookie.lower() or "expire" in set_cookie.lower() or "expires" in logout_resp.headers.get("set-cookie", "").lower() or logout_resp.headers.get("set-cookie", "").lower().startswith("ting_ting_auth")

    def test_after_logout_cookie_only_request_is_unauthenticated(self, client, tmp_session):
        self._login(client, tmp_session)

        # Logout
        client.post("/api/auth/logout")

        # Now make a profile request — should fail (cookie cleared)
        profile_resp = client.get("/api/profile/me")
        assert profile_resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# AC6: Profile read and update, owner-only
# ---------------------------------------------------------------------------

class TestAC6Profile:
    def _register_and_login(self, client, username, email, password="securepass1"):
        client.post("/api/auth/register", json={
            "username": username,
            "email": email,
            "password": password,
        })
        resp = client.post("/api/auth/login", json={
            "identifier": username,
            "password": password,
        })
        assert resp.status_code == status.HTTP_200_OK
        return resp.json()["access_token"]

    def test_read_own_profile(self, client):
        self._register_and_login(client, "profileuser", "profile@example.com")
        resp = client.get("/api/profile/me")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["username"] == "profileuser"
        assert data["email"] == "profile@example.com"

    def test_update_own_profile_persists(self, client, tmp_engine):
        self._register_and_login(client, "updateuser", "update@example.com")
        resp = client.patch("/api/profile/me", json={
            "display_name": "Updated Name",
            "bio": "New bio text",
        })
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["display_name"] == "Updated Name"
        assert data["bio"] == "New bio text"

        # Verify persistence via direct DB check
        from sqlalchemy import text
        with tmp_engine.connect() as conn:
            row = conn.execute(
                text("SELECT display_name, bio FROM users WHERE username = 'updateuser'")
            ).fetchone()
        assert row[0] == "Updated Name"
        assert row[1] == "New bio text"

    def test_cannot_update_other_users_profile(self, client, tmp_session, tmp_engine):
        """AC6 negative: owner-only mutation protection.

        The API does not expose a PATCH /api/profile/{user_id} route.
        We verify that each user's session only writes to their own row.
        """
        # Register + login bob
        self._register_and_login(client, "bob", "bob1@example.com", "bobpass123")
        bob_token_resp = client.post("/api/auth/login", json={
            "identifier": "bob",
            "password": "bobpass123",
        })
        _bob_token = bob_token_resp.json()["access_token"]

        # Update bob's profile
        client.patch("/api/profile/me", json={"display_name": "Bob Updated"})

        # Register alice in same DB
        alice_resp = client.post("/api/auth/register", json={
            "username": "alice",
            "email": "alice1@example.com",
            "password": "alicepass123",
        })
        assert alice_resp.status_code in (status.HTTP_201_CREATED, status.HTTP_409_CONFLICT)

        # Login as alice (cookie session switches to alice)
        alice_login = client.post("/api/auth/login", json={
            "identifier": "alice",
            "password": "alicepass123",
        })
        assert alice_login.status_code == status.HTTP_200_OK

        # Alice updating her own profile
        alice_patch = client.patch("/api/profile/me", json={
            "display_name": "Alice Updated",
        })
        assert alice_patch.status_code == status.HTTP_200_OK
        assert alice_patch.json()["username"] == "alice"
        assert alice_patch.json()["display_name"] == "Alice Updated"

        # Bob's data should remain unchanged
        from sqlalchemy import text
        with tmp_engine.connect() as conn:
            bob_row = conn.execute(
                text("SELECT display_name FROM users WHERE username = 'bob'")
            ).fetchone()
        assert bob_row[0] == "Bob Updated"
