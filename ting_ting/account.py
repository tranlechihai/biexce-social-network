"""Account lifecycle — data export, self-deactivation, deletion (T-023).

* :func:`export_account` — one-shot JSON of everything the user owns
  (data portability). Only rows the user authored/created are included;
  no other account's secrets ever appear.
* :func:`deactivate_account` / :func:`reactivate_account` — a reversible
  self-service pause. Deactivation revokes every session (immediate
  everywhere sign-out) but does NOT block sign-in: the user may return,
  then reactivate. While deactivated the account is hidden from other
  users (feeds, search, public profile, graphs).
* :func:`delete_account` — the irreversible end of that flow. Physically
  removes the user and every row they own, anonymizes moderation reports
  that reference them (evidence retention), and writes a :class:`DeletedAccount`
  tombstone reserving username + email for :data:`TOMBSTONE_WINDOW_DAYS`.

Datetime convention: SQLite stores naive UTC wall-clock strings, so every
SQL-level TTL comparison below normalizes to naive UTC.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ting_ting.models import (
    Activity, Block, Comment, DeletedAccount, Follow, FriendRequest,
    Like, Mute, Post, PostMedia,
    Repost, Report, SavedPost, User, UserProfile, UserWarning,
)
from ting_ting import sessions as session_service
from ting_ting.auth import verify_password

#: How long a deleted account's username/email stay reserved.
TOMBSTONE_WINDOW_DAYS = 30


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

    from ting_ting import notifications as notification_service

    warnings = [
        {
            "id": warning.id,
            "report_id": warning.report_id,
            "reason": warning.reason,
            "note": warning.note,
            "created_at": _dt(warning.created_at),
        }
        for warning in db.scalars(
            select(UserWarning).where(UserWarning.user_id == user.id)
            .order_by(UserWarning.created_at.asc(), UserWarning.id.asc())
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
            "banned_until": _dt(user.banned_until),
            "ban_reason": user.ban_reason,
            "role": user.role,
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
        "notification_preferences": notification_service.get_preferences(db, user.id),
        "moderation_warnings": warnings,
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


# ---------------------------------------------------------------------------
# Deletion — irreversible
# ---------------------------------------------------------------------------

def _naive_utc(dt: datetime) -> datetime:
    """Normalize to the naive UTC wall-clock form SQLite stores/compares."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def tombstone_cutoff(now: datetime | None = None) -> datetime:
    """Naive-UTC boundary: tombstones deleted at/after it are still fresh."""
    now = now or datetime.now(timezone.utc)
    return _naive_utc(now) - timedelta(days=TOMBSTONE_WINDOW_DAYS)


def assert_credentials_available(
    db: Session, username: str, email: str, now: datetime | None = None,
) -> None:
    """Reject identifiers locked by a fresh deletion tombstone.

    Raises ``ValueError("username_taken")`` / ``ValueError("email_taken")``.
    Expired tombstones do NOT block registration; their rows are purged by
    the T-030 jobs worker.
    """
    cutoff = tombstone_cutoff(now)
    if db.scalar(
        select(func.count()).select_from(DeletedAccount).where(
            DeletedAccount.username == username,
            DeletedAccount.deleted_at > cutoff,
        )
    ):
        raise ValueError("username_taken")
    if db.scalar(
        select(func.count()).select_from(DeletedAccount).where(
            DeletedAccount.email == email,
            DeletedAccount.deleted_at > cutoff,
        )
    ):
        raise ValueError("email_taken")


def delete_account(
    db: Session, user: User, password: str, now: datetime | None = None,
) -> list[str]:
    """Delete the account and ALL its content — irreversible.

    * Requires the current password (``ValueError("invalid_password")``).
    * Anonymizes reports that reference the user (user refs -> NULL) so the
      moderation evidence/audit survives the deletion for the retention
      window (enforced at read time by :mod:`ting_ting.moderation`).
    * Deletes every row the user owns or participates in (content, graph
      edges, notifications), letting DB-level cascades finish the rest.
    * Revoke-all sessions, then writes a :class:`DeletedAccount` tombstone
      reserving username + email for :data:`TOMBSTONE_WINDOW_DAYS`.

    Returns the on-disk media paths (post media + avatar) the caller must
    unlink AFTER commit — the file-vs-DB ordering discipline of
    ``mod_delete_post`` (commit first; a failed unlink leaves a reclaimable
    orphan that ``scripts/reconcile.py`` reports, never loses live data).
    """
    if not verify_password(password, user.password_hash):
        raise ValueError("invalid_password")

    now = now or datetime.now(timezone.utc)
    u = user.id
    username = user.username
    email = user.email

    # Media files are addressed from DB rows — collect the paths first.
    media_paths = list(db.scalars(
        select(PostMedia.path)
        .join(Post, PostMedia.post_id == Post.id)
        .where(Post.author_id == u)
        .order_by(PostMedia.id.asc())
    ).all())
    profile = db.get(UserProfile, u)
    avatar = (profile.avatar_path or profile.avatar_url) if profile else None
    if avatar:
        media_paths.append(avatar)

    # 1) Reports: keep them as anonymized evidence. NULL only the refs that
    #    point at this user (CASE — never a blanket wipe of other refs).
    db.execute(
        Report.__table__.update()
        .where(or_(Report.reporter_id == u, Report.resolved_by == u))
        .values(
            reporter_id=case((Report.reporter_id == u, None), else_=Report.reporter_id),
            resolved_by=case((Report.resolved_by == u, None), else_=Report.resolved_by),
        )
    )
    db.execute(
        Report.__table__.update()
        .where(Report.target_user_id == u)
        .values(target_user_id=None)
    )

    # 2) Rows referencing the user (both directions) that have no DB cascade —
    #    explicit deletes. Ordering: comments before posts so reply chains
    #    attach to the post cascade.
    db.execute(
        Activity.__table__.delete()
        .where(or_(Activity.user_id == u, Activity.actor_id == u))
    )
    db.execute(SavedPost.__table__.delete().where(SavedPost.user_id == u))
    db.execute(Repost.__table__.delete().where(Repost.user_id == u))
    db.execute(Like.__table__.delete().where(Like.user_id == u))
    db.execute(Comment.__table__.delete().where(Comment.author_id == u))
    db.execute(
        FriendRequest.__table__.delete()
        .where(or_(FriendRequest.sender_id == u, FriendRequest.recipient_id == u))
    )
    db.execute(
        Block.__table__.delete()
        .where(or_(Block.blocker_id == u, Block.blocked_id == u))
    )
    db.execute(
        Mute.__table__.delete()
        .where(or_(Mute.muted_by == u, Mute.target_id == u))
    )
    db.execute(
        Follow.__table__.delete()
        .where(or_(Follow.follower_id == u, Follow.followed_id == u))
    )

    # 3) The user's posts — DB cascades remove the comments/likes/media/
    #    saved/reposts/notifications/mutes pinned to those posts; report
    #    pins SET NULL.
    db.execute(Post.__table__.delete().where(Post.author_id == u))

    # 4) 1:1 profile row (no cascade).
    db.execute(UserProfile.__table__.delete().where(UserProfile.user_id == u))

    # 5) Revoke every session (sessions + refresh_tokens cascade on the
    #    user delete below; this kills them explicitly first so the JWT
    #    validation path sees nothing).
    session_service.revoke_all_sessions(db, u)

    # 6) Reserve the identifiers. Any pre-existing tombstone row under the
    #    same name can only be an expired one (a live user owns the unique
    #    name), so replace it.
    db.execute(
        DeletedAccount.__table__.delete().where(
            or_(DeletedAccount.username == username, DeletedAccount.email == email)
        )
    )
    db.add(DeletedAccount(username=username, email=email, deleted_at=now))

    # 7) The user row itself — last, so every FK referring side is clean.
    db.delete(user)
    db.flush()
    return media_paths
