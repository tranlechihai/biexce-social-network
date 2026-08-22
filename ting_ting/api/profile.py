"""Profile endpoints — read own profile, update own basic profile fields."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ting_ting import social
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import User
from ting_ting.schemas import (
    AvatarUploadResponse,
    ProfileUpdateRequest,
    UserResponse,
)

router = APIRouter(prefix="/profile", tags=["profile"])


@router.post("/avatar", response_model=AvatarUploadResponse)
async def upload_my_avatar(
    avatar_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AvatarUploadResponse:
    """Replace the authenticated user's avatar (owner-only, T-025).

    Multipart field ``avatar_file``; images only (JPEG/PNG/WebP, ≤ 2 MB),
    dangerous-content scanned and quota-checked like post media.  The
    previous avatar file is removed after the replacement is committed.
    """
    from ting_ting.uploads import ingest_avatar

    new_path, error_code = await ingest_avatar(db, current_user, avatar_file)
    if error_code is not None:
        status_code = 413 if error_code in {"quota_exceeded", "storage_full"} else 422
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": error_code,
                "message": {
                    "media_too_large": "Avatar must be at most 2 MB.",
                    "invalid_media": "Avatar must be a JPEG, PNG or WebP image.",
                    "blocked_content": "The file contains prohibited content and was rejected.",
                    "quota_exceeded": "Storage quota exceeded.",
                    "storage_full": "Server storage quota exceeded.",
                }.get(error_code, error_code),
            },
        )
    return AvatarUploadResponse(avatar_url=new_path)


@router.get(
    "/me",
    response_model=UserResponse,
)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Return the authenticated user's own profile."""
    return UserResponse.model_validate(current_user)


@router.patch(
    "/me",
    response_model=UserResponse,
)
async def update_my_profile(
    body: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Update editable basic profile fields (owner-only).

    T-024: ``is_private`` — making the account private changes how others
    can follow (new follows become pending requests); going public
    auto-approves every pending follow request (side effects in
    ``social.apply_privacy_change``).
    """
    if body.display_name is not None:
        current_user.display_name = body.display_name
    if body.bio is not None:
        current_user.bio = body.bio
    if body.is_private is not None:
        social.apply_privacy_change(db, current_user, body.is_private)

    db.commit()
    db.refresh(current_user)
    return UserResponse.model_validate(current_user)
