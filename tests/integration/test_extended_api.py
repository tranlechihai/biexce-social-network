"""Contract tests for API parity with extended web features."""

from pathlib import Path

from ting_ting.models import Activity, Follow, PostMedia, Repost, SavedPost


def _register(client, username: str) -> dict:
    response = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "password123",
    })
    assert response.status_code == 201
    return response.json()


def _auth(client, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={
        "identifier": username,
        "password": "password123",
    })
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _post(client, headers: dict[str, str], audience: str = "ONLY_ME") -> int:
    response = client.post(
        "/api/posts",
        json={"content": "extended API post", "audience": audience},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_extended_profile_round_trip(client):
    _register(client, "profile_api")
    headers = _auth(client, "profile_api")

    response = client.patch("/api/profile/me/details", headers=headers, json={
        "display_name": "Profile API",
        "bio": "Extended",
        "birthday": "2000-01-02",
        "gender": "prefer_not_to_say",
        "location": "Da Nang",
        "occupation": "Engineer",
        "website": "https://example.com",
    })

    assert response.status_code == 200
    assert response.json()["location"] == "Da Nang"
    assert response.json()["website"] == "https://example.com"
    assert client.get("/api/profile/me/details", headers=headers).json() == response.json()


def test_extended_profile_rejects_non_http_url(client):
    _register(client, "profile_invalid")
    headers = _auth(client, "profile_invalid")

    response = client.patch(
        "/api/profile/me/details",
        headers=headers,
        json={"website": "javascript:alert(1)"},
    )

    assert response.status_code == 422


def test_follow_is_idempotent_and_creates_one_activity(client, tmp_session):
    target = _register(client, "follow_target")
    _register(client, "follow_actor")
    actor_headers = _auth(client, "follow_actor")

    first = client.put(f"/api/social/follows/{target['id']}", headers=actor_headers)
    second = client.put(f"/api/social/follows/{target['id']}", headers=actor_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert tmp_session.query(Follow).count() == 1
    assert tmp_session.query(Activity).filter_by(kind="follow").count() == 1
    assert len(client.get("/api/social/following", headers=actor_headers).json()) == 1

    target_headers = _auth(client, "follow_target")
    assert len(client.get("/api/social/followers", headers=target_headers).json()) == 1
    activity = client.get("/api/activity", headers=target_headers).json()
    assert activity[0]["kind"] == "follow"
    assert activity[0]["actor"]["username"] == "follow_actor"


def test_block_prevents_follow(client, tmp_session):
    target = _register(client, "blocked_target")
    actor = _register(client, "blocked_actor")
    target_headers = _auth(client, "blocked_target")
    actor_headers = _auth(client, "blocked_actor")
    assert client.post(
        "/api/social/blocks",
        headers=target_headers,
        json={"target_user_id": actor["id"]},
    ).status_code == 201

    response = client.put(f"/api/social/follows/{target['id']}", headers=actor_headers)

    assert response.status_code == 409
    assert tmp_session.query(Follow).count() == 0


def test_block_removes_existing_follow(client, tmp_session):
    target = _register(client, "remove_follow_target")
    actor = _register(client, "remove_follow_actor")
    actor_headers = _auth(client, "remove_follow_actor")
    target_headers = _auth(client, "remove_follow_target")
    client.put(f"/api/social/follows/{target['id']}", headers=actor_headers)
    assert tmp_session.query(Follow).count() == 1

    response = client.post(
        "/api/social/blocks",
        headers=target_headers,
        json={"target_user_id": actor["id"]},
    )

    assert response.status_code == 201
    assert tmp_session.query(Follow).count() == 0
    assert client.get("/api/activity", headers=target_headers).json() == []


def test_unfollow_is_idempotent(client, tmp_session):
    target = _register(client, "unfollow_target")
    _register(client, "unfollow_actor")
    headers = _auth(client, "unfollow_actor")
    client.put(f"/api/social/follows/{target['id']}", headers=headers)

    assert client.delete(f"/api/social/follows/{target['id']}", headers=headers).status_code == 200
    assert client.delete(f"/api/social/follows/{target['id']}", headers=headers).status_code == 200
    assert tmp_session.query(Follow).count() == 0


def test_saved_post_and_repost_are_idempotent(client, tmp_session):
    _register(client, "post_features")
    headers = _auth(client, "post_features")
    post_id = _post(client, headers)

    assert client.put(f"/api/posts/{post_id}/saved", headers=headers).status_code == 200
    assert client.put(f"/api/posts/{post_id}/saved", headers=headers).status_code == 200
    assert client.put(f"/api/posts/{post_id}/repost", headers=headers).status_code == 200
    assert client.put(f"/api/posts/{post_id}/repost", headers=headers).status_code == 200
    assert tmp_session.query(SavedPost).count() == 1
    assert tmp_session.query(Repost).count() == 1
    assert len(client.get("/api/saved", headers=headers).json()) == 1

    assert client.delete(f"/api/posts/{post_id}/saved", headers=headers).status_code == 200
    assert client.delete(f"/api/posts/{post_id}/repost", headers=headers).status_code == 200
    assert tmp_session.query(SavedPost).count() == 0
    assert tmp_session.query(Repost).count() == 0


def test_cannot_save_invisible_post(client):
    _register(client, "private_owner")
    owner_headers = _auth(client, "private_owner")
    post_id = _post(client, owner_headers)
    _register(client, "private_stranger")
    stranger_headers = _auth(client, "private_stranger")

    response = client.put(f"/api/posts/{post_id}/saved", headers=stranger_headers)

    assert response.status_code == 404


def test_post_media_upload_and_delete(client, tmp_session):
    _register(client, "media_api")
    headers = _auth(client, "media_api")
    post_id = _post(client, headers)

    response = client.post(
        f"/api/posts/{post_id}/media",
        headers=headers,
        files={"file": ("photo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
    )

    assert response.status_code == 201
    media_id = response.json()["id"]
    file_path = Path("uploads", Path(response.json()["url"]).name)
    assert file_path.exists()
    assert client.get(response.json()["url"], headers=headers).status_code == 200

    deleted = client.delete(f"/api/posts/{post_id}/media/{media_id}", headers=headers)
    assert deleted.status_code == 200
    assert tmp_session.query(PostMedia).count() == 0
    assert not file_path.exists()


def test_media_upload_requires_post_owner(client):
    _register(client, "media_owner")
    owner_headers = _auth(client, "media_owner")
    post_id = _post(client, owner_headers, audience="FRIENDS")
    _register(client, "media_other")
    other_headers = _auth(client, "media_other")

    response = client.post(
        f"/api/posts/{post_id}/media",
        headers=other_headers,
        files={"file": ("photo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
    )

    assert response.status_code == 403


def test_media_upload_rejects_spoofed_content(client, tmp_session):
    _register(client, "media_invalid")
    headers = _auth(client, "media_invalid")
    post_id = _post(client, headers)

    response = client.post(
        f"/api/posts/{post_id}/media",
        headers=headers,
        files={"file": ("photo.png", b"not-an-image", "image/png")},
    )

    assert response.status_code == 422
    assert tmp_session.query(PostMedia).count() == 0
