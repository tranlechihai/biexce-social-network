"""Account lifecycle — data export and self-deactivation (T-023).

* :func:`export_account` — one-shot JSON of everything the user owns
  (data portability). Only rows the user authored/created are included;
  no other account's secrets ever appear.
* :func:`deactivate_account` / :func:`reactivate_account` — a reversible
  self-service pause. Deactivation revokes every session (immediate
  everywhere sign-out) but does NOT block sign-in: the user may return,
  then reactivate. While deactivated the account is hidden from other
  users (feeds, search, public profile, graphs).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting.models import (
    Activity, Comment, Follow, Like, Post, PostMedia,
    Repost, SavedPost, User, UserProfile,
)
from ting_ting import sessions as session_service
from ting_ting.auth import verify_password


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_account(db: Session, user: User) -> dict:
    """Build the account export document for ``user`` (their data only)."""
    profile = db.get(UserProfile, user.id)

    posts = []
    for post in db.scalars(
        select(Post).where(Post.author_id == user.id)
        .order_by(Post.created_at.asc(), Post.id.asc())
    ).all():
        media = [
            m.path
            for m in db.scalars(
                select(PostMedia).where(PostMedia.post_id == post.id)
                .order_by(PostMedia.id.asc())
            ).all()
        ]
        posts.append({
            "id": post.id,
            "content": post.content,
            "audience": post.audience,
            "created_at": _dt(post.created_at),
            "updated_at": _dt(post.updated_at),
            "media": media,
        })

    comments = [
        {
            "id": c.id,
            "post_id": c.post_id,
            "parent_comment_id": c.parent_comment_id,
            "content": c.content,
            "created_at": _dt(c.created_at),
        }
        for c in db.scalars(
            select(Comment).where(Comment.author_id == user.id)
            .order_by(Comment.created_at.asc(), Comment.id.asc())
        ).all()
    ]

    liked = [r[0] for r in db.execute(
        select(Like.post_id).where(Like.user_id == user.id)
        .order_by(Like.post_id.asc())
    )]
    saved = [r[0] for r in db.execute(
        select(SavedPost.post_id).where(SavedPost.user_id == user.id)
        .order_by(SavedPost.post_id.asc())
    )]
    reposted = [r[0] for r in db.execute(
        select(Repost.post_id).where(Repost.user_id == user.id)
        .order_by(Repost.post_id.asc())
    )]
    following = [r[0] for r in db.execute(
        select(Follow.followed_id).where(Follow.follower_id == user.id)
        .order_by(Follow.followed_id.asc())
    )]
    followers = [r[0] for r in db.execute(
        select(Follow.follower_id).where(Follow.followed_id == user.id)
        .order_by(Follow.follower_id.asc())
    )]

    # Notifications the user received (public context only — actor ids, not
    # actor emails).
    activities = [
        {
            "id": a.id,
            "kind": a.kind,
            "actor_id": a.actor_id,
            "post_id": a.post_id,
            "read_at": _dt(a.read_at),
            "created_at": _dt(a.created_at),
        }
        for a in db.scalars(
            select(Activity).where(Activity.user_id == user.id)
            .order_by(Activity.created_at.asc(), Activity.id.asc())
        ).all()
    ]

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "bio": user.bio,
            "banned_at": _dt(user.banned_at),
            "deactivated_at": _dt(user.deactivated_at),
        },
        "profile": {
            "birthday": profile.birthday if profile else None,
            "gender": profile.gender if profile else None,
            "location": profile.location if profile else None,
            "occupation": profile.occupation if profile else None,
            "website": profile.website if profile else None,
            "avatar_url": (profile.avatar_path or profile.avatar_url) if profile else None,
        },
        "posts": posts,
        "comments": comments,
        "liked_post_ids": liked,
        "saved_post_ids": saved,
        "reposted_post_ids": reposted,
        "following_user_ids": following,
        "follower_user_ids": followers,
        "notifications": activities,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


def deactivate_account(db: Session, user: User, password: str) -> None:
    """Reversibly deactivate ``user``.

    Requires the current password (defense against an authenticated
    XSS/CSRF acting on a hijacked browser). Revokes ALL sessions so every
    device is signed out immediately. Raises ``ValueError("invalid_password")``.
    """
    if not verify_password(password, user.password_hash):
        raise ValueError("invalid_password")

    now = datetime.now(timezone.utc)
    user.deactivated_at = now
    session_service.revoke_all_sessions(db, user.id)
    db.flush()


def reactivate_account(db: Session, user: User) -> None:
    """Lift a self-deactivation (auth required at the endpoint layer)."""
    user.deactivated_at = None
    db.flush()

