"""Notification REST API — list, unread count, mark read.

Endpoints (all require authentication and only ever expose the caller's own
notifications):

* GET  /api/notifications                  — cursor-paginated, filterable list
* GET  /api/notifications/unread-count      — unread count (block-filtered)
* POST /api/notifications/{activity_id}/read  — mark one read
* POST /api/notifications/read-all          — mark all read

Notifications from blocked actors (either direction) are hidden from every
endpoint, consistent with the web activity feed.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting import notifications
from ting_ting.models import User
from ting_ting.schemas import (
    NotificationListResponse,
    NotificationResponse,
    UnreadCountResponse,
    UserRef,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _item(db: Session, row) -> NotificationResponse:
    actor = db.get(User, row.actor_id)
    return NotificationResponse(
        id=row.id,
        actor=_user_ref(actor) if actor else UserRef(id=row.actor_id, username="unknown"),
        kind=row.kind,
        post_id=row.post_id,
        created_at=row.created_at.isoformat() if row.created_at else None,
        is_read=row.read_at is not None,
    )


@router.get("", response_model=NotificationListResponse)
def list_notifications_api(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(
        default=None,
        pattern="^(follow|like|comment|repost)$",
    ),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """List my notifications, newest first, with keyset (cursor) pagination."""
    try:
        rows, next_cursor = notifications.list_notifications(
            db, me.id, limit=limit, cursor=cursor, kind=kind,
        )
    except ValueError as exc:
        if str(exc) == "invalid kind":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "validation", "message": "Invalid kind filter."},
            ) from None
        raise
    return NotificationListResponse(
        items=[_item(db, row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count_api(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Unread notification count, excluding blocked actors."""
    return UnreadCountResponse(unread=notifications.unread_count(db, me.id))


@router.post("/{activity_id}/read", response_model=NotificationResponse)
def mark_read_api(
    activity_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Mark one of my notifications read (idempotent)."""
    row = db.scalar(select(notifications.Activity).where(notifications.Activity.id == activity_id))
    if row is None or row.user_id != me.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Notification not found."},
        )
    notifications.mark_read(db, me.id, row)
    db.commit()
    return _item(db, row)


@router.post("/read-all")
def mark_all_read_api(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Mark all of my unread notifications read."""
    updated = notifications.mark_all_read(db, me.id)
    db.commit()
    return {"updated": updated}
