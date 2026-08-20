"""Integration tests for the UI polish increment: profile follow counts,
composer upload preview/progress markup, confirmation dialogs, feed errors."""
import pytest
from fastapi import status

pytestmark = pytest.mark.integration


def _login(client, username):
    client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@tt.com", "password": "securepass1",
    })
    client.post("/api/auth/login", json={"identifier": username, "password": "securepass1"})


class TestProfileFollowCounts:
    def test_counts_shown_on_other_profile(self, client):
        # Register all three first, so lookups never 404 mid-way.
        _login(client, "uicnt_a")
        _login(client, "uicnt_b")
        _login(client, "uicnt_c")
        # a follows b
        _login(client, "uicnt_a")
        client.post(
            "/web/social/follow", data={"target_username": "uicnt_b"},
            follow_redirects=False,
        )
        # c also follows b
        _login(client, "uicnt_c")
        client.post(
            "/web/social/follow", data={"target_username": "uicnt_b"},
            follow_redirects=False,
        )
        html = client.get("/web/profile/uicnt_b").text
        # b follows nobody, 2 people follow b
        assert "Đang theo dõi" in html
        assert "Người theo dõi" in html
        import re
        stats = re.search(r"profile-stats.*?</p>", html, re.S).group(0)
        assert re.search(r"<strong>0</strong>\s*Đang theo dõi", stats)
        assert re.search(r"<strong>2</strong>\s*Người theo dõi", stats)

    def test_own_profile_shows_counts(self, client):
        _login(client, "uicnt_me")
        html = client.get("/web/profile/me").text
        assert "Đang theo dõi" in html
        assert "Người theo dõi" in html

    def test_blocked_profile_hides_counts(self, client):
        _login(client, "uicnt_x")
        _login(client, "uicnt_y")
        client.post(
            "/web/social/follow", data={"target_username": "uicnt_x"},
            follow_redirects=False,
        )
        # y blocks x, then x views y (blocked pair -> redacted)
        client.post("/web/social/block", data={"target_username": "uicnt_x"},
                    follow_redirects=False)
        _login(client, "uicnt_x")
        html = client.get("/web/profile/uicnt_y").text
        assert "profile-stats" not in html


class TestComposerUploadUi:
    def test_composer_has_preview_and_progress_hooks(self, client):
        _login(client, "uicomposer1")
        html = client.get("/web/feed").text
        assert 'data-upload-form' in html
        assert 'id="composer-preview"' in html
        assert 'id="composer-upload-state"' in html
        assert "URL.createObjectURL" in html
        assert "xhr.upload.onprogress" in html
        assert "Đang tải file lên" in html

    def test_upload_failed_error_renders_on_feed(self, client):
        _login(client, "uicomposer2")
        html = client.get("/web/feed?error=upload_failed").text
        assert "Đăng bài thất bại" in html


class TestConfirmationDialogs:
    def test_delete_post_asks_confirmation(self, client):
        _login(client, "uiconfirm1")
        client.post(
            "/web/posts/create", data={"content": "delete me", "audience": "PUBLIC"},
            follow_redirects=False,
        )
        html = client.get("/web/feed").text
        assert 'confirm(' in html
        assert "Hành động không thể hoàn tác" in html

    def test_block_user_asks_confirmation(self, client):
        _login(client, "uiconfirm_a")
        _login(client, "uiconfirm_b")
        profile = client.get("/web/profile/uiconfirm_a").text
        assert "confirm('Chặn uiconfirm_a" in profile

    def test_unfriend_asks_confirmation(self, client):
        _login(client, "uiunf_a")
        _login(client, "uiunf_b")
        # b sends a a friend request, a accepts
        client.post("/web/social/friend-request", data={"target_username": "uiunf_a"},
                    follow_redirects=False)
        _login(client, "uiunf_a")
        from sqlalchemy import select
        from ting_ting.database import get_session_factory
        from ting_ting.models import FriendRequest
        session = get_session_factory()()
        pending = session.execute(
            select(FriendRequest).where(FriendRequest.state == "pending"),
        ).scalars().first()
        session.close()
        assert pending is not None
        click = client.post("/web/social/accept-request",
                            data={"request_id": str(pending.id)}, follow_redirects=False)
        assert click.status_code == status.HTTP_303_SEE_OTHER
        html = client.get("/web/profile/uiunf_b").text
        assert "Ngừng là bạn với uiunf_b" in html
