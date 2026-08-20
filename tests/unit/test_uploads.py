"""Unit tests for upload validation, dangerous-content scan and quotas (P0.4)."""
import pytest

from ting_ting.config import Settings
from ting_ting.uploads import (
    UploadRejected,
    check_upload_quota,
    detect_media,
    scan_dangerous,
    total_storage_bytes,
    user_storage_bytes,
    validate_upload_bytes,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 32
PE = b"MZ\x90\x00" + b"\x00" * 32
ELF = b"\x7fELF" + b"\x00" * 32
ZIP = b"PK\x03\x04" + b"\x00" * 32
PDF = b"%PDF-1.7" + b"\x00" * 32
SHEBANG = b"#!/bin/sh\necho hi"


def _settings(**kw):
    base = {"jwt_secret": "test-secret"}
    base.update(kw)
    return Settings(**base)


class TestDetect:
    def test_recognized_containers(self):
        assert detect_media(PNG) == (".png", "image")
        assert detect_media(JPG) == (".jpg", "image")
        assert detect_media(WEBP) == (".webp", "image")
        assert detect_media(MP4) == (".mp4", "video")
        assert detect_media(WEBM) == (".webm", "video")

    def test_unknown_rejected(self):
        assert detect_media(b"plain text") is None
        assert detect_media(b"") is None


class TestScan:
    @pytest.mark.parametrize("payload,label", [
        (PE, "PE executable"),
        (ELF, "ELF executable"),
        (ZIP, "ZIP archive"),
        (PDF, "PDF document"),
        (SHEBANG, "script shebang"),
    ])
    def test_blocked_markers(self, payload, label):
        assert scan_dangerous(payload) == label

    def test_clean_payloads(self):
        for data in (PNG, JPG, WEBP, MP4, WEBM):
            assert scan_dangerous(data) is None

    def test_payload_appended_after_valid_header(self):
        # Classic trick: valid image header, executable appended behind it.
        assert scan_dangerous(PNG + PE) is not None
        assert scan_dangerous(PNG + ZIP) is not None


class TestValidate:
    def test_allows_image_and_video(self):
        assert validate_upload_bytes(PNG, 1024, allow_video=True)[1] == "image"
        assert validate_upload_bytes(MP4, 1024, allow_video=True)[1] == "video"

    def test_avatars_reject_video(self):
        with pytest.raises(UploadRejected) as exc:
            validate_upload_bytes(MP4, 1024, allow_video=False)
        assert exc.value.code == "invalid_media"

    def test_rejects_wrong_type(self):
        with pytest.raises(UploadRejected) as exc:
            validate_upload_bytes(b"random bytes", 1024)
        assert exc.value.code == "invalid_media"

    def test_rejects_oversized(self):
        with pytest.raises(UploadRejected) as exc:
            validate_upload_bytes(PNG, 10)
        assert exc.value.code == "media_too_large"

    def test_rejects_embedded_dangerous_content(self):
        with pytest.raises(UploadRejected) as exc:
            validate_upload_bytes(PNG + PE, 4096)
        assert exc.value.code == "blocked_content"

    def test_rejects_empty(self):
        with pytest.raises(UploadRejected) as exc:
            validate_upload_bytes(b"", 1024)
        assert exc.value.code == "invalid_media"


class TestQuota:
    def test_user_storage_counts_only_own_files(self, tmp_path):
        (tmp_path / "post-7-a.png").write_bytes(b"x" * 100)
        (tmp_path / "avatar-7-b.png").write_bytes(b"x" * 50)
        (tmp_path / "post-8-c.png").write_bytes(b"x" * 999)
        assert user_storage_bytes(tmp_path, 7) == 150
        assert user_storage_bytes(tmp_path, 8) == 999
        assert total_storage_bytes(tmp_path) == 1149

    def test_quota_exceeded_raises(self, tmp_path):
        (tmp_path / "post-7-a.png").write_bytes(b"x" * 1000)
        settings = _settings(upload_quota_mb=0.001)  # ~1048 bytes
        with pytest.raises(UploadRejected) as exc:
            check_upload_quota(tmp_path, 7, 50, settings)  # 1000 + 50 > 1048
        assert exc.value.code == "quota_exceeded"

    def test_within_quota_passes(self, tmp_path):
        (tmp_path / "post-7-a.png").write_bytes(b"x" * 1000)
        settings = _settings(upload_quota_mb=512)
        check_upload_quota(tmp_path, 7, 10, settings)  # no raise

    def test_fleet_quota_raises(self, tmp_path):
        (tmp_path / "post-7-a.png").write_bytes(b"x" * 900)
        settings = _settings(upload_quota_mb=512, total_upload_quota_mb=0.001)
        with pytest.raises(UploadRejected) as exc:
            check_upload_quota(tmp_path, 7, 200, settings)
        assert exc.value.code == "storage_full"

    def test_missing_dir_treated_as_empty(self, tmp_path):
        settings = _settings()
        check_upload_quota(tmp_path / "nope", 1, 10, settings)  # no raise
