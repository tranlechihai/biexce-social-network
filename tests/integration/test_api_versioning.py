"""Integration tests for API versioning: /api/v1 canonical, /api deprecated."""
import pytest

from fastapi import status

pytestmark = pytest.mark.integration


def _login(client, username, password="securepass1"):
    resp = client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@tt.com", "password": password,
    })
    assert resp.status_code == status.HTTP_201_CREATED
    token = client.post(
        "/api/v1/auth/login", json={"identifier": username, "password": password},
    ).json()
    return f"Bearer {token['access_token']}"


class TestV1Endpoints:
    def test_register_login_profile_on_v1(self, client):
        auth = _login(client, "v1user01")
        legacy_me = client.get("/api/profile/me", headers={"Authorization": auth}).json()
        v1_resp = client.get("/api/v1/profile/me", headers={"Authorization": auth})
        assert v1_resp.status_code == status.HTTP_200_OK
        v1_me = v1_resp.json()
        assert v1_me["id"] == legacy_me["id"]
        assert v1_me["username"] == "v1user01"

    def test_posts_flow_on_v1(self, client):
        auth = _login(client, "v1user02")
        h = {"Authorization": auth}
        created = client.post(
            "/api/v1/posts", json={"content": "hello v1", "audience": "PUBLIC"},
        )
        assert created.status_code == status.HTTP_201_CREATED
        post_id = created.json()["id"]
        feed = client.get("/api/v1/feed", headers=h)
        assert feed.status_code == status.HTTP_200_OK
        assert any(p["id"] == post_id for p in feed.json())
        liked = client.post(f"/api/v1/posts/{post_id}/likes", headers=h)
        assert liked.status_code == status.HTTP_200_OK
        assert liked.json()["like_count"] == 1

    def test_notifications_and_users_on_v1(self, client):
        a = _login(client, "v1user03a")
        b = _login(client, "v1user03b")
        search = client.get("/api/v1/users", params={"q": "v1user03b"},
                            headers={"Authorization": a})
        assert search.status_code == status.HTTP_200_OK
        items = search.json()
        if isinstance(items, dict):
            items = items.get("items", [])
        assert any(u["username"] == "v1user03b" for u in items)
        notif = client.get("/api/v1/notifications", headers={"Authorization": b})
        assert notif.status_code == status.HTTP_200_OK


class TestDeprecationPolicy:
    def test_legacy_paths_carry_deprecation_headers(self, client):
        resp = client.post(
            "/api/auth/login", json={"identifier": "nobody", "password": "wrong"},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert resp.headers.get("Deprecation") == "true"
        assert "/api/v1" in resp.headers.get("Warning", "")

    def test_v1_paths_are_clean(self, client):
        resp = client.post(
            "/api/v1/auth/login", json={"identifier": "nobody", "password": "wrong"},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Deprecation" not in resp.headers
        assert "Warning" not in resp.headers

    def test_media_is_not_versioned(self, client):
        # File delivery keeps its stable /media path on both v0 and v1 eras.
        schema = client.get("/openapi.json").json()["paths"]
        assert "/media/{filename}" in schema
        assert "/api/v1/media/{filename}" not in schema
        assert "/api/posts" in schema  # legacy still documented

    def test_openapi_documents_both_generations(self, client):
        schema = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/auth/login" in schema
        assert "/api/auth/login" in schema
        assert "/api/v1/feed" in schema
        assert "/api/feed" in schema
