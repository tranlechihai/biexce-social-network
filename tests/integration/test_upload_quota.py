"""Integration tests for upload quota, content scan, error surfacing and CSP (P0.4)."""
import pytest

from fastapi import status

from ting_ting.config import Settings

PNG_MID = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x00" * 400    # 1211 B
PNG_BIG = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x00" * 1200   # 3611 B
PE_PAYLOAD = b"MZ\x90\x00" + b"\x00" * 64


@pytest.fixture()
def isolated_uploads(tmp_path, monkeypatch):
    """Point the uploads dir at a temp folder and apply a tiny quota."""
    import ting_ting.config as config_mod
    import ting_ting.media as media_mod

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(media_mod, "UPLOADS_DIR", uploads)
    tiny = Settings(jwt_secret="test", upload_quota_mb=0.004)  # ~4 KB
    # media.py imports get_settings at top level; web helpers import it at
    # call time from ting_ting.config — cover both resolution paths.
    monkeypatch.setattr(config_mod, "_default_settings", tiny)
    monkeypatch.setattr(media_mod, "get_settings", lambda: tiny)
    return uploads


def _login(client, username, password="securepass1"):
    resp = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@tt.com", "password": password,
    })
    assert resp.status_code == status.HTTP_201_CREATED
    token = client.post(
        "/api/auth/login", json={"identifier": username, "password": password},
    ).json()
    return f"Bearer {token['access_token']}"


def _create_post_id(client, auth):
    resp = client.post(
        "/api/posts", json={"content": "media target", "audience": "PUBLIC"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()["id"]


class TestApiMediaQuota:
    def test_upload_within_quota(self, client, isolated_uploads):
        auth = _login(client, "apiup01")
        post_id = _create_post_id(client, auth)
        resp = client.post(
            f"/api/posts/{post_id}/media",
            files={"file": ("ok.png", PNG_MID, "image/png")},
            headers={"Authorization": auth},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["url"].startswith("/media/post-")
        files = list(isolated_uploads.iterdir())
        assert len(files) == 1 and files[0].name.startswith("post-")

    def test_upload_over_quota_413(self, client, isolated_uploads):
        auth = _login(client, "apiup02")
        post_id = _create_post_id(client, auth)
        ok = client.post(
            f"/api/posts/{post_id}/media", files={"file": ("ok.png", PNG_MID, "image/png")},
            headers={"Authorization": auth},
        )
        assert ok.status_code == status.HTTP_201_CREATED
        blocked = client.post(
            f"/api/posts/{post_id}/media", files={"file": ("big.png", PNG_BIG, "image/png")},
            headers={"Authorization": auth},
        )
        assert blocked.status_code == 413
        assert blocked.json()["error"]["code"] == "quota_exceeded"

    def test_embedded_executable_rejected_422(self, client, isolated_uploads):
        auth = _login(client, "apiup03")
        post_id = _create_post_id(client, auth)
        resp = client.post(
            f"/api/posts/{post_id}/media",
            files={"file": ("tricky.png", PNG_MID + PE_PAYLOAD, "image/png")},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "blocked_content"


class TestWebMediaErrorSurfacing:
    def test_web_post_quota_redirects_with_error(self, client, isolated_uploads):
        client.post("/api/auth/register", json={
            "username": "webup01", "email": "webup01@tt.com", "password": "securepass1",
        })
        client.post("/api/auth/login", json={"identifier": "webup01", "password": "securepass1"})
        # First upload OK (fills most of the ~4 KB quota).
        r1 = client.post(
            "/web/posts/create",
            data={"content": "first", "audience": "PUBLIC"},
            files={"media_file": ("big.png", PNG_BIG, "image/png")},
            follow_redirects=False,
        )
        assert r1.status_code == status.HTTP_303_SEE_OTHER
        # Second exceeds quota -> redirect with stable error code.
        r2 = client.post(
            "/web/posts/create",
            data={"content": "second", "audience": "PUBLIC"},
            files={"media_file": ("again.png", PNG_MID, "image/png")},
            follow_redirects=False,
        )
        assert r2.status_code == status.HTTP_303_SEE_OTHER
        assert "error=quota_exceeded" in r2.headers["location"]
        # Following the redirect, the feed renders a human message instead of
        # failing silently.
        feed = client.get(r2.headers["location"]).text
        assert "hạn mức lưu trữ cá nhân" in feed

    def test_web_post_blocked_content_redirects_with_error(self, client, isolated_uploads):
        client.post("/api/auth/register", json={
            "username": "webup02", "email": "webup02@tt.com", "password": "securepass1",
        })
        client.post("/api/auth/login", json={"identifier": "webup02", "password": "securepass1"})
        resp = client.post(
            "/web/posts/create",
            data={"content": "evil", "audience": "PUBLIC"},
            files={"media_file": ("evil.png", PNG_MID + PE_PAYLOAD, "image/png")},
            follow_redirects=False,
        )
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=blocked_content" in resp.headers["location"]
        assert "bị cấm" in client.get(resp.headers["location"]).text


class TestWebAvatarQuota:
    def test_avatar_over_quota_renders_error(self, client, isolated_uploads):
        client.post("/api/auth/register", json={
            "username": "webup03", "email": "webup03@tt.com", "password": "securepass1",
        })
        client.post("/api/auth/login", json={"identifier": "webup03", "password": "securepass1"})
        # Fill the quota with post media first, then the avatar must fail.
        client.post(
            "/web/posts/create",
            data={"content": "filler", "audience": "PUBLIC"},
            files={"media_file": ("filler.png", PNG_MID, "image/png")},
            follow_redirects=False,
        )
        resp = client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("me.png", PNG_BIG, "image/png")},
            follow_redirects=False,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "hạn mức lưu trữ cá nhân" in resp.text
        assert "/media/avatar-" not in resp.text


class TestCspHeader:
    def test_pages_carry_csp(self, client):
        resp = client.get("/web/login")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    def test_api_responses_carry_csp(self, client):
        resp = client.post(
            "/api/auth/login", json={"identifier": "nobody", "password": "wrong"},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert "default-src 'self'" in resp.headers.get("content-security-policy", "")
