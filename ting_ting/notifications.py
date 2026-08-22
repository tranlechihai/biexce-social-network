"""Notification service — the single writer/reader for ``Activity`` rows.

Every feature (web *and* API) creates notifications through this module so the
two surfaces stay consistent:

* ``record`` — preference-aware, race-safe idempotent creation.
* ``list_notifications`` — cursor-paginated, block-filtered listing.
* ``unread_count`` / ``mark_read`` / ``mark_all_read`` — read-state helpers.

Rules:
* Self-interactions never notify (``actor_id == user_id``).
* Retries of the same interaction do not duplicate: while an unread row for
  the same (user, actor, kind, post) exists, ``record`` returns it as-is.
* Any block between viewer and actor hides the notification (both directions).
"""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting.keyset import decode_cursor, encode_cursor
from ting_ting.models import Activity, Block, Mute, NotificationPreference, User

NOTIFICATION_KINDS = (
    "follow", "follow_request", "like", "comment", "repost", "mention",
)
AGGREGATED_KINDS = ("like", "comment", "repost")
_PREFERENCE_FIELDS = {
    kind: f"{kind}_enabled" for kind in NOTIFICATION_KINDS
}


def get_preferences(db: Session, user_id: int) -> dict[str, bool]:
    row = db.get(NotificationPreference, user_id)
    if row is None:
        return {kind: True for kind in NOTIFICATION_KINDS}
    return {
        kind: bool(getattr(row, field))
        for kind, field in _PREFERENCE_FIELDS.items()
    }


def update_preferences(
    db: Session,
    user_id: int,
    changes: dict[str, bool],
) -> dict[str, bool]:
    if any(kind not in NOTIFICATION_KINDS for kind in changes):
        raise ValueError("invalid kind")
    row = db.get(NotificationPreference, user_id)
    if row is None:
        candidate = None
        try:
            with db.begin_nested():
                candidate = NotificationPreference(user_id=user_id)
                db.add(candidate)
                db.flush()
        except IntegrityError:
            if candidate is not None:
                from sqlalchemy.orm.attributes import instance_state
                if instance_state(candidate).session_id is not None:
                    db.expunge(candidate)
            row = db.get(NotificationPreference, user_id)
            if row is None:
                raise
        else:
            row = candidate
    for kind, enabled in changes.items():
        setattr(row, _PREFERENCE_FIELDS[kind], enabled)
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return get_preferences(db, user_id)


def is_enabled(db: Session, user_id: int, kind: str) -> bool:
    if kind not in NOTIFICATION_KINDS:
        raise ValueError("invalid kind")
    row = db.get(NotificationPreference, user_id)
    return row is None or bool(getattr(row, _PREFERENCE_FIELDS[kind]))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def record(
    db: Session,
    user_id: int,
    actor_id: int,
    kind: str,
    post_id: int | None = None,
    *,
    source_key: str | None = None,
) -> Activity | None:
    """Create a notification row for ``user_id`` about ``actor_id``'s action.

    Returns the created/existing winner row, or ``None`` when no notification
    is created (self action or recipient preference disabled).
    """
    if kind not in NOTIFICATION_KINDS:
        raise ValueError("invalid kind")
    if user_id == actor_id or not is_enabled(db, user_id, kind):
        return None

    if source_key is None:
        source_key = f"post:{post_id}" if post_id is not None else "edge"
    source_key = source_key[:96]

    existing = db.execute(
        select(Activity).where(
            Activity.user_id == user_id,
            Activity.actor_id == actor_id,
            Activity.kind == kind,
            Activity.source_key == source_key,
            Activity.read_at.is_(None),
        ).order_by(Activity.id.desc())
    ).scalars().first()
    if existing is not None:
        return existing

    activity = None
    try:
        with db.begin_nested():
            activity = Activity(
                user_id=user_id, actor_id=actor_id, kind=kind,
                post_id=post_id, source_key=source_key,
            )
            db.add(activity)
            db.flush()
    except IntegrityError:
        # Another transaction won the unread-key race. The savepoint keeps
        # the outer use-case alive; converge to the persisted winner.
        if activity is not None:
            from sqlalchemy.orm.attributes import instance_state
            if instance_state(activity).session_id is not None:
                db.expunge(activity)
        return db.execute(
            select(Activity).where(
                Activity.user_id == user_id,
                Activity.actor_id == actor_id,
                Activity.kind == kind,
                Activity.source_key == source_key,
                Activity.read_at.is_(None),
            ).order_by(Activity.id.desc())
        ).scalars().first()
    return activity


def _visible_conditions(user_id: int):
    blocked = (
        select(Block.blocked_id).where(Block.blocker_id == user_id)
        .union_all(select(Block.blocker_id).where(Block.blocked_id == user_id))
    )
    muted = select(Mute.target_id).where(
        Mute.muted_by == user_id, Mute.post_id.is_(None),
    )
    return [Activity.actor_id.not_in(blocked), Activity.actor_id.not_in(muted)]


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

    rows = list(db.scalars(
        select(Activity)
        .where(*conditions, *_visible_conditions(user_id))
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(limit + 1)
    ).all())
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = encode_cursor(visible[-1]) if has_more else None
    return visible, next_cursor


def unread_count(db: Session, user_id: int) -> int:
    """Count unread notifications from actors the user has not blocked or muted."""
    return int(db.scalar(
        select(func.count(Activity.id)).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
            *_visible_conditions(user_id),
        )
    ) or 0)


@dataclass
class NotificationAggregate:
    kind: str
    post_id: int
    latest: Activity
    actors: list[User]
    actor_count: int
    event_count: int
    aggregation_key: str


def _aggregate_key(kind: str, post_id: int, cutoff_id: int) -> str:
    raw = f"{kind}|{post_id}|{cutoff_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_aggregate_key(value: str) -> tuple[str, int, int]:
    try:
        padded = value + "=" * (-len(value) % 4)
        kind, post_id, cutoff_id = base64.urlsafe_b64decode(padded).decode().split("|")
        if kind not in AGGREGATED_KINDS:
            raise ValueError
        return kind, int(post_id), int(cutoff_id)
    except Exception as exc:
        raise ValueError("invalid aggregate key") from exc


def list_aggregates(
    db: Session,
    user_id: int,
    limit: int = 20,
) -> list[NotificationAggregate]:
    """Newest unread post-interaction groups; raw history stays unchanged."""
    summary_rows = db.execute(
        select(
            Activity.kind,
            Activity.post_id,
            func.max(Activity.id).label("latest_id"),
            func.count(Activity.id).label("event_count"),
            func.count(func.distinct(Activity.actor_id)).label("actor_count"),
        ).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
            Activity.kind.in_(AGGREGATED_KINDS),
            Activity.post_id.is_not(None),
            *_visible_conditions(user_id),
        ).group_by(Activity.kind, Activity.post_id)
        .order_by(func.max(Activity.created_at).desc(), func.max(Activity.id).desc())
        .limit(limit)
    ).all()
    if not summary_rows:
        return []

    clauses = [
        (Activity.kind == row.kind) & (Activity.post_id == row.post_id)
        for row in summary_rows
    ]
    members = db.scalars(
        select(Activity).where(
            Activity.user_id == user_id,
            Activity.read_at.is_(None),
            or_(*clauses),
            *_visible_conditions(user_id),
        ).order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all()
    by_group: dict[tuple[str, int], list[Activity]] = {}
    for member in members:
        by_group.setdefault((member.kind, member.post_id), []).append(member)
    actor_ids = {row.actor_id for row in members}
    users = {
        user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids))).all()
    }

    result = []
    for summary in summary_rows:
        group = by_group[(summary.kind, summary.post_id)]
        distinct_actors = []
        seen = set()
        for member in group:
            actor = users.get(member.actor_id)
            if actor is not None and actor.id not in seen:
                seen.add(actor.id)
                distinct_actors.append(actor)
                if len(distinct_actors) == 3:
                    break
        latest = group[0]
        result.append(NotificationAggregate(
            kind=summary.kind,
            post_id=summary.post_id,
            latest=latest,
            actors=distinct_actors,
            actor_count=int(summary.actor_count),
            event_count=int(summary.event_count),
            aggregation_key=_aggregate_key(summary.kind, summary.post_id, latest.id),
        ))
    return result


def mark_aggregate_read(db: Session, user_id: int, key: str) -> int:
    kind, post_id, cutoff_id = _decode_aggregate_key(key)
    rows = db.scalars(select(Activity).where(
        Activity.user_id == user_id,
        Activity.kind == kind,
        Activity.post_id == post_id,
        Activity.id <= cutoff_id,
        Activity.read_at.is_(None),
    )).all()
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    for row in rows:
        row.read_at = now
    db.flush()
    return len(rows)


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
