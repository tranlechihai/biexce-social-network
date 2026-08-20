"""Security regressions for the first production-hardening phase."""

from pathlib import Path

from ting_ting.models import Activity, Follow, Post, PostMedia, User
from ting_ting.config import get_settings
from ting_ting.security import RateLimiter


def _register(client, username: str) -> None:
    client.get("/web/register")
    token = client.cookies.get("ting_ting_csrf")
    client.post(
        "/web/register",
        data={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
            "csrf_token": token,
        },
    )


def test_web_post_without_csrf_is_rejected(client):
    client.get("/web/register")

    response = client.post(
        "/web/register",
        data={
            "username": "attacker",
            "email": "attacker@example.com",
            "password": "password123",
        },
        headers={"X-CSRF-Token": ""},
    )

    assert response.status_code == 403


def test_security_headers_are_present(client):
    response = client.get("/web/login")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_registration_rate_limit_returns_429(client):
    import ting_ting.main as main_module

    settings = get_settings()
    previous = settings.rate_limit_enabled
    previous_limiter = main_module.rate_limiter
    settings.rate_limit_enabled = True
    main_module.rate_limiter = RateLimiter()
    try:
        for index in range(10):
            response = client.post("/api/auth/register", json={
                "username": f"limited_{index}",
                "email": f"limited_{index}@example.com",
                "password": "password123",
            })
            assert response.status_code == 201

        response = client.post("/api/auth/register", json={
            "username": "limited_final",
            "email": "limited_final@example.com",
            "password": "password123",
        })
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
    finally:
        settings.rate_limit_enabled = previous
        main_module.rate_limiter = previous_limiter


def test_uploaded_media_requires_current_post_visibility(client, tmp_session):
    _register(client, "owner")
    response = client.post(
        "/web/posts/create",
        data={
            "content": "private image",
            "audience": "ONLY_ME",
            "csrf_token": client.cookies.get("ting_ting_csrf"),
        },
        files={"media_file": ("photo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    media = tmp_session.query(PostMedia).one()
    filename = Path(media.path).name
    assert client.get(f"/media/{filename}").status_code == 200

    client.post(
        "/web/logout",
        data={"csrf_token": client.cookies.get("ting_ting_csrf")},
    )
    assert client.get(f"/media/{filename}").status_code == 401

    Path("uploads", filename).unlink(missing_ok=True)


def test_blocked_user_cannot_follow(client, tmp_session):
    _register(client, "alice_secure")
    _register(client, "bob_secure")
    bob = tmp_session.query(User).filter_by(username="bob_secure").one()

    client.post(
        "/web/social/block",
        data={
            "target_username": "alice_secure",
            "csrf_token": client.cookies.get("ting_ting_csrf"),
        },
    )
    client.post(
        "/web/social/follow",
        data={
            "target_username": "alice_secure",
            "csrf_token": client.cookies.get("ting_ting_csrf"),
        },
    )

    assert tmp_session.query(Follow).filter_by(follower_id=bob.id).count() == 0


def test_replayed_like_creates_one_activity(client, tmp_session):
    _register(client, "author_secure")
    author = tmp_session.query(User).filter_by(username="author_secure").one()
    post = Post(author_id=author.id, content="activity", audience="FRIENDS")
    tmp_session.add(post)
    tmp_session.commit()

    _register(client, "reader_secure")
    reader = tmp_session.query(User).filter_by(username="reader_secure").one()
    from ting_ting.models import FriendRequest

    left, right = sorted((author.id, reader.id))
    tmp_session.add(FriendRequest(
        sender_id=author.id,
        recipient_id=reader.id,
        canonical_left=left,
        canonical_right=right,
        state="accepted",
    ))
    tmp_session.commit()

    payload = {"csrf_token": client.cookies.get("ting_ting_csrf")}
    client.post(f"/web/posts/{post.id}/like", data=payload)
    client.post(f"/web/posts/{post.id}/like", data=payload)

    assert tmp_session.query(Activity).filter_by(
        user_id=author.id,
        actor_id=reader.id,
        kind="like",
        post_id=post.id,
    ).count() == 1


def test_deleting_post_removes_uploaded_file(client, tmp_session):
    _register(client, "cleanup")
    client.post(
        "/web/posts/create",
        data={
            "content": "delete me",
            "audience": "ONLY_ME",
            "csrf_token": client.cookies.get("ting_ting_csrf"),
        },
        files={"media_file": ("photo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
    )
    post = tmp_session.query(Post).filter_by(content="delete me").one()
    media = tmp_session.query(PostMedia).filter_by(post_id=post.id).one()
    file_path = Path("uploads", Path(media.path).name)
    assert file_path.exists()

    client.post(
        f"/web/posts/{post.id}/delete",
        data={"csrf_token": client.cookies.get("ting_ting_csrf")},
    )

    assert not file_path.exists()
