"""User discovery REST API — search, public profile, followers/following.

Endpoints:
* GET /api/users                              — keyset-paginated user search
* GET /api/users/{username}                   — public profile (relationship
                                                + counts, block-redacted)
* GET /api/users/{username}/followers         — who follows the user
* GET /api/users/{username}/following         — who the user follows

Visibility rules match the web surface: a user who blocked the viewer is
excluded from search; a blocked pair gets a 404 on the profiles' graphs and
redacted fields on the public profile itself.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ting_ting import social
from ting_ting.keyset import decode_cursor, encode_cursor
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import Follow, User, UserProfile
from ting_ting.schemas import (
    UserPublicResponse,
    UserRef,
    UserSearchItem,
    FEED_LIMIT_MIN,
    FEED_LIMIT_MAX,
    FEED_LIMIT_DEFAULT,
)

router = APIRouter(prefix="/users", tags=["users"])

NEXT_CURSOR_HEADER = "X-Next-Cursor"


def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _avatar_url(profile: UserProfile | None) -> str | None:
    return (profile.avatar_path or profile.avatar_url) if profile else None


def _find_by_username(db: Session, username: str) -> User:
    """404 for missing users and banned users — a ban removes the account
    from public discovery without leaking that it existed."""
    user = db.scalar(
        select(User)
        .where(User.username == username)
        .where(User.banned_at.is_(None))
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "User not found."},
        )
    return user


@router.get("", response_model=list[UserSearchItem])
def search_users(
    response: Response,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
    q: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=FEED_LIMIT_DEFAULT, ge=FEED_LIMIT_MIN, le=FEED_LIMIT_MAX),
    cursor: str | None = Query(default=None),
):
    """Search users by username/display name (keyset pagination).

    Excludes the viewer and users who blocked the viewer.
    """
    page, next_cursor = social.search_users(
        db, me.id, query=q, limit=limit, cursor=cursor,
    )
    db.commit()
    followed_ids = set(
        db.scalars(
            select(Follow.followed_id).where(Follow.follower_id == me.id)
        ).all()
    )
    items = []
    for user in page:
        items.append(UserSearchItem(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            relationship=social.relationship_state(db, me.id, user.id),
            followed=user.id in followed_ids,
        ))
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return items


@router.get("/{username}", response_model=UserPublicResponse)
def get_public_user(
    username: str,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Public profile of another user with relationship and counts.

    Blocked pairs see identity + relationship only (redacted profile).
    """
    target = _find_by_username(db, username)
    rel = social.relationship_state(db, me.id, target.id)

    if rel in ("blocked_by_me", "blocked_by_them"):
        # Block privacy: identity + relationship only, nothing else leaks.
        return UserPublicResponse(
            id=target.id,
            username=target.username,
            relationship=rel,
        )

    if target.deactivated_at is not None and target.id != me.id:
        # Self-deactivated (T-023): hidden from everyone but the owner.
        # Redacted (not 404) so a previously-seen profile does not break.
        return UserPublicResponse(
            id=target.id,
            username=target.username,
            relationship="none",
        )

    profile = db.get(UserProfile, target.id)
    counts = social.user_counts(db, target.id)
    db.commit()
    return UserPublicResponse(
        id=target.id,
        username=target.username,
        display_name=target.display_name,
        bio=target.bio,
        avatar_url=_avatar_url(profile),
        relationship=rel,
        follower_count=counts["followers"],
        following_count=counts["following"],
        friend_count=counts["friends"],
    )


def _followers_or_404(
    db: Session,
    me: User,
    username: str,
    direction: str,
    limit: int | None = None,
    cursor: str | None = None,
    response: Response | None = None,
) -> list[UserRef]:
    target = _find_by_username(db, username)
    if social.is_blocked(db, me.id, target.id):
        # Hide the graph entirely for blocked pairs (no existence leaks).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "User not found."},
        )
    if target.deactivated_at is not None and target.id != me.id:
        # A self-deactivated user's graph is off-limits too (T-023).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "User not found."},
        )
    query = select(Follow).where(
        Follow.followed_id == target.id
        if direction == "followers"
        else Follow.follower_id == target.id
    )
    if cursor:
        try:
            created_at, row_id = decode_cursor(cursor)
        except ValueError:
            cursor = None  # malformed cursor → start over, never a 500
        if cursor:
            query = query.where(
                or_(
                    Follow.created_at < created_at,
                    and_(Follow.created_at == created_at, Follow.id < row_id),
                )
            )
    # Bounded over-fetch keeps pages full even when rows are skipped
    # (deleted/banned/blocked accounts); without a limit we return the
    # whole visible graph, as before.
    if limit is not None:
        query = query.limit(limit * 2 + 4)
    rows = db.scalars(
        query.order_by(Follow.created_at.desc(), Follow.id.desc())
    ).all()
    other_column = "follower_id" if direction == "followers" else "followed_id"
    results: list[UserRef] = []
    consumed = 0
    last_row: Follow | None = None
    for row in rows:
        consumed += 1
        user = db.get(User, getattr(row, other_column))
        if (
            user is not None
            and user.banned_at is None
            and user.deactivated_at is None  # T-023: hide self-deactivated
            and not social.is_blocked(db, me.id, user.id)
        ):
            results.append(_user_ref(user))
            last_row = row
            if limit is not None and len(results) == limit:
                if consumed < len(rows) and response is not None:
                    response.headers[NEXT_CURSOR_HEADER] = encode_cursor(last_row)
                db.commit()
                return results
    db.commit()
    return results


@router.get("/{username}/followers", response_model=list[UserRef])
def get_user_followers(
    response: Response,
    username: str,
    limit: int | None = Query(default=None, ge=FEED_LIMIT_MIN, le=FEED_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Who follows the user — 404 for blocked pairs.

    Optional keyset pagination (T-022): pass ``limit`` and feed the
    ``X-Next-Cursor`` response header back as ``cursor``.
    """
    return _followers_or_404(db, me, username, "followers", limit, cursor, response)


@router.get("/{username}/following", response_model=list[UserRef])
def get_user_following(
    response: Response,
    username: str,
    limit: int | None = Query(default=None, ge=FEED_LIMIT_MIN, le=FEED_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Who the user follows — 404 for blocked pairs.

    Optional keyset pagination (T-022): pass ``limit`` and feed the
    ``X-Next-Cursor`` response header back as ``cursor``.
    """
    return _followers_or_404(db, me, username, "following", limit, cursor, response)
