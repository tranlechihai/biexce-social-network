"""Profile endpoints — read own profile, update own basic profile fields."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ting_ting import social
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import User
from ting_ting.schemas import ProfileUpdateRequest, UserResponse

router = APIRouter(prefix="/profile", tags=["profile"])


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
