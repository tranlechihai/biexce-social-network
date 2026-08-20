"""Notification service — the single writer/reader for ``Activity`` rows.

Every feature (web *and* API) creates notifications through this module so the
two surfaces stay consistent:

* ``record`` — idempotent, self-safe notification creation.
* ``list_notifications`` — cursor-paginated, block-filtered listing.
* ``unread_count`` / ``mark_read`` / ``mark_all_read`` — read-state helpers.

Rules:
* Self-interactions never notify (``actor_id == user_id``).
* Retries of the same interaction do not duplicate: while an unread row for
  the same (user, actor, kind, post) exists, ``record`` returns it as-is.
* Any block between viewer and actor hides the notification (both directions).
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ting_ting import social
from ting_ting.keyset import decode_cursor, encode_cursor
from ting_ting.models import Activity, Mute

NOTIFICATION_KINDS = ("follow", "like", "comment", "repost")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def record(
    db: Session,
    user_id: int,
    actor_id: int,
    kind: str,
    post_id: int | None = None,
) -> Activity | None:
    """Create a notification row for ``user_id`` about ``actor_id``'s action.

    Returns the row, or ``None`` when no notification is created (self action,
    or an unread notification for the exact same interaction already exists).
    """
    if kind not in NOTIFICATION_KINDS:
        raise ValueError("invalid kind")
    if user_id == actor_id:
        return None

    existing = db.execute(
        select(Activity).where(
            Activity.user_id == user_id,
            Activity.actor_id == actor_id,
            Activity.kind == kind,
            Activity.post_id == post_id if post_id is not None else Activity.post_id.is_(None),
            Activity.read_at.is_(None),
        ).order_by(Activity.id.desc())
    ).scalars().first()
    if existing is not None:
        return existing

    activity = Activity(user_id=user_id, actor_id=actor_id, kind=kind, post_id=post_id)
    db.add(activity)
    db.flush()
    return activity


def _newer_condition(cursor: str):
    created_at, row_id = decode_cursor(cursor)
    from sqlalchemy import and_, or_
    return or_(
        Activity.created_at < created_at,
        and_(Activity.created_at == created_at, Activity.id < row_id),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_notifications(
    db: Session,
    user_id: int,
    limit: int = 20,
    cursor: str | None = None,
    kind: str | None = None,
) -> tuple[list[Activity], str | None]:
    """Return one page of notifications, newest first.

    Notifications whose actor is blocked (either direction) are hidden and do
    NOT count toward ``limit`` — the page is filled with visible rows only.
    Returns ``(rows, next_cursor)``; ``next_cursor`` is ``None`` on the last
    page.
    """
    if kind is not None and kind not in NOTIFICATION_KINDS:
        raise ValueError("invalid kind")

    conditions = [Activity.user_id == user_id]
    if kind is not None:
        conditions.append(Activity.kind == kind)
    if cursor:
        try:
            conditions.append(_newer_condition(cursor))
        except ValueError:
            cursor = None  # malformed cursor → start over, never a 500

    muted = set(
        db.scalars(
            select(Mute.target_id).where(
                Mute.muted_by == user_id, Mute.post_id.is_(None),
            )
        ).all()
    )

    rows = db.scalars(
        select(Activity)
        .where(*conditions)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(limit * 4 + 4)
    ).all()

    visible = [
        row for row in rows
        if not social.is_blocked(db, user_id, row.actor_id)
        and row.actor_id not in muted
    ][:limit]
    next_cursor = encode_cursor(visible[-1]) if len(visible) == limit and len(rows) > limit else None
    return visible, next_cursor


def unread_count(db: Session, user_id: int) -> int:
    """Count unread notifications from actors the user has not blocked or muted."""
    total = db.scalar(
        select(func.count(Activity.id)).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
            Activity.actor_id.not_in(
                select(Mute.target_id).where(
                    Mute.muted_by == user_id, Mute.post_id.is_(None),
                )
            ),
        )
    ) or 0
    if total == 0:
        return 0
    rows = db.scalars(
        select(Activity.actor_id).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
        )
    ).all()
    hidden = 0
    for actor_id in set(rows):
        if social.is_blocked(db, user_id, actor_id):
            hidden += db.scalar(
                select(func.count(Activity.id)).where(
                    Activity.user_id == user_id,
                    Activity.actor_id == actor_id,
                    Activity.read_at.is_(None),
                )
            ) or 0
    return max(total - hidden, 0)


def mark_read(db: Session, user_id: int, activity: Activity) -> bool:
    """Mark one notification read. Returns ``False`` if it did not belong to
    the user (or was already read)."""
    if activity is None or activity.user_id != user_id or activity.read_at is not None:
        return False
    activity.read_at = datetime.now(timezone.utc)
    db.flush()
    return True


def mark_all_read(db: Session, user_id: int) -> int:
    """Mark every unread notification for the user read; returns rows updated."""
    rows = db.scalars(
        select(Activity).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.read_at = now
    db.flush()
    return len(rows)
