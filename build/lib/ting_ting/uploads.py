"""Upload validation, dangerous-content scan, and storage quotas.

Single source of truth for every path that accepts user-uploaded bytes
(API post media, web post media, avatar upload):

* signature validation — only JPEG/PNG/WebP (and MP4/WebM for post media);
* dangerous-content scan — executable/archive/document markers are rejected
  anywhere in the payload (not only at offset 0), which catches payloads
  appended behind a valid image header;
* storage quotas — per-user and fleet-wide limits measured on actual disk
  usage of the uploads directory (filenames embed the owning user id).

Quota settings: ``TING_UPLOAD_QUOTA_MB`` (per user, default 512),
``TING_TOTAL_UPLOAD_QUOTA_MB`` (fleet, default 5120).
"""

from pathlib import Path

MAX_POST_MEDIA = 25 * 1024 * 1024
AVATAR_MAX = 2 * 1024 * 1024


class UploadRejected(Exception):
    """Raised when an upload cannot be accepted.

    ``code`` is a stable machine-readable identifier (API error envelope /
    web error parameter): ``media_too_large``, ``invalid_media``,
    ``blocked_content``, ``quota_exceeded``, ``storage_full``.
    """

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or {"quota_exceeded": "Storage quota exceeded.",
                                   "storage_full": "Server storage quota exceeded."}.get(code, code)
        super().__init__(self.message)


def detect_media(data: bytes) -> tuple[str, str] | None:
    """Return ``(suffix, media_type)`` for a recognized container, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp", "image"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".mp4", "video"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".webm", "video"
    return None


# 4-byte markers only: in compressed image/video payloads any 2-3 byte
# sequence appears by chance, so short markers would false-positive.
_BLOCKED_MARKERS = (
    (b"MZ\x90\x00", "PE executable"),
    (b"\x7fELF", "ELF executable"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"%PDF", "PDF document"),
    (b"\xca\xfe\xba\xbe", "Java class"),
    (b"\xd0\xcf\x11\xe0", "OLE2 compound file"),
)


def scan_dangerous(data: bytes) -> str | None:
    """Return a human reason when dangerous content is present, else None."""
    if data.startswith(b"#!"):
        return "script shebang"
    for marker, label in _BLOCKED_MARKERS:
        if marker in data:
            return label
    return None


def validate_upload_bytes(data: bytes, max_bytes: int, allow_video: bool = True) -> tuple[str, str]:
    """Validate raw upload bytes. Returns ``(suffix, media_type)``.

    Raises :class:`UploadRejected` with a stable ``code`` on failure.
    """
    if not data:
        raise UploadRejected("invalid_media")
    if len(data) > max_bytes:
        raise UploadRejected("media_too_large")
    danger = scan_dangerous(data)
    if danger is not None:
        raise UploadRejected("blocked_content", f"Rejected content: {danger}")
    detected = detect_media(data)
    if detected is None or (detected[1] == "video" and not allow_video):
        raise UploadRejected("invalid_media")
    return detected


def user_storage_bytes(uploads_dir: Path, user_id: int) -> int:
    """Bytes on disk owned by a user (upload filenames embed the user id)."""
    if not uploads_dir.is_dir():
        return 0
    total = 0
    for pattern in (f"post-{user_id}-*", f"avatar-{user_id}-*"):
        for candidate in uploads_dir.glob(pattern):
            if candidate.is_file():
                total += candidate.stat().st_size
    return total


def total_storage_bytes(uploads_dir: Path) -> int:
    if not uploads_dir.is_dir():
        return 0
    return sum(p.stat().st_size for p in uploads_dir.iterdir() if p.is_file())


def check_upload_quota(
    uploads_dir: Path,
    user_id: int,
    adding_bytes: int,
    settings,
) -> None:
    """Raise :class:`UploadRejected` if the upload would break a quota."""
    per_user = int(getattr(settings, "upload_quota_mb", 512) * 1024 * 1024)
    fleet = int(getattr(settings, "total_upload_quota_mb", 5120) * 1024 * 1024)
    if user_storage_bytes(uploads_dir, user_id) + adding_bytes > per_user:
        raise UploadRejected(
            "quota_exceeded",
            f"Personal storage quota ({per_user / 1024 / 1024:g} MB) exceeded.",
        )
    if total_storage_bytes(uploads_dir) + adding_bytes > fleet:
        raise UploadRejected(
            "storage_full",
            "Server storage quota exceeded.",
        )
