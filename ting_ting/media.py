"""Authorized delivery and lifecycle helpers for locally stored media."""

import os
from pathlib import Path

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting import posts, social
from ting_ting.auth import get_current_user
from ting_ting.config import get_settings
from ting_ting.database import get_db
from ting_ting.models import Post, PostMedia, User, UserProfile
from ting_ting.uploads import (
    MAX_POST_MEDIA, check_upload_quota, validate_upload_bytes,
)


router = APIRouter(tags=["media"])


def _resolve_uploads_dir() -> Path:
    """Media storage directory.

    Containers set ``TING_UPLOADS_DIR`` to the mounted volume (e.g.
    ``/app/uploads``); otherwise the dev layout ``<repo>/uploads`` is used.
    Relative values resolve against the CWD.
    """
    value = os.environ.get("TING_UPLOADS_DIR")
    if value:
        path = Path(value)
        return path if path.is_absolute() else Path.cwd() / path
    return Path(__file__).resolve().parent.parent / "uploads"


UPLOADS_DIR = _resolve_uploads_dir()


async def store_post_upload(
    upload: UploadFile,
    prefix: str,
    user_id: int,
    max_bytes: int = MAX_POST_MEDIA,
) -> tuple[str, str]:
    """Validate, quota-check, and scan one image/video under a server
    generated name.  Raises :class:`UploadRejected` (stable ``code``)."""
    data = await upload.read(max_bytes + 1)
    check_upload_quota(UPLOADS_DIR, user_id, len(data) if data else 0, get_settings())
    suffix, media_type = validate_upload_bytes(data, max_bytes, allow_video=True)

    UPLOADS_DIR.mkdir(exist_ok=True)
    filename = f"{prefix}-{uuid4().hex}{suffix}"
    (UPLOADS_DIR / filename).write_bytes(data)
    return f"/media/{filename}", media_type


def stored_file_path(stored_path: str) -> Path | None:
    filename = Path(stored_path).name
    if not filename:
        return None
    candidate = (UPLOADS_DIR / filename).resolve()
    if candidate.parent != UPLOADS_DIR.resolve():
        return None
    return candidate


def delete_stored_file(stored_path: str) -> None:
    path = stored_file_path(stored_path)
    if path is not None:
        path.unlink(missing_ok=True)


def _authorized_file(filename: str, db: Session, viewer: User) -> Path:
    if Path(filename).name != filename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found.")

    media = db.scalar(select(PostMedia).where(PostMedia.path.like(f"%/{filename}")))
    if media is not None:
        post = db.get(Post, media.post_id)
        if post is not None and posts.is_visible_to(post.author_id, viewer.id, post.audience, db):
            path = stored_file_path(media.path)
            if path is not None and path.is_file():
                return path

    profile = db.scalar(
        select(UserProfile).where(
            UserProfile.avatar_path.like(f"%/{filename}")
        )
    )
    if profile is not None and not social.is_blocked(db, viewer.id, profile.user_id):
        path = stored_file_path(profile.avatar_path or profile.avatar_url or "")
        if path is not None and path.is_file():
            return path

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found.")


@router.get("/media/{filename}")
@router.get("/uploads/{filename}", include_in_schema=False)
def get_media(
    filename: str,
    db: Session = Depends(get_db),
    viewer: User = Depends(get_current_user),
):
    """Deliver media only while the viewer retains access to its owner/post."""
    path = _authorized_file(filename, db, viewer)
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})
