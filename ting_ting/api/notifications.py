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
    AggregateReadResponse,
    NotificationAggregateListResponse,
    NotificationAggregateResponse,
    NotificationListResponse,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
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


@router.get("/preferences", response_model=NotificationPreferencesResponse)
def get_notification_preferences(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    return NotificationPreferencesResponse(**notifications.get_preferences(db, me.id))


@router.patch("/preferences", response_model=NotificationPreferencesResponse)
def patch_notification_preferences(
    body: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    values = body.model_dump(exclude_none=True)
    result = notifications.update_preferences(db, me.id, values)
    db.commit()
    return NotificationPreferencesResponse(**result)


@router.get("/aggregates", response_model=NotificationAggregateListResponse)
def list_notification_aggregates(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    groups = notifications.list_aggregates(db, me.id, limit=limit)
    return NotificationAggregateListResponse(items=[
        NotificationAggregateResponse(
            id=group.latest.id,
            actor=(
                _user_ref(group.actors[0]) if group.actors
                else UserRef(id=group.latest.actor_id, username="unknown")
            ),
            actors=[_user_ref(actor) for actor in group.actors],
            actor_count=group.actor_count,
            event_count=group.event_count,
            kind=group.kind,
            post_id=group.post_id,
            created_at=(
                group.latest.created_at.isoformat() if group.latest.created_at else None
            ),
            aggregation_key=group.aggregation_key,
        )
        for group in groups
    ])


@router.post(
    "/aggregates/{aggregation_key}/read",
    response_model=AggregateReadResponse,
)
def mark_notification_aggregate_read(
    aggregation_key: str,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    try:
        updated = notifications.mark_aggregate_read(db, me.id, aggregation_key)
    except ValueError:
        updated = 0
    if updated == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Notification aggregate not found."},
        )
    db.commit()
    return AggregateReadResponse(updated=updated)


@router.get("", response_model=NotificationListResponse)
def list_notifications_api(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(
        default=None,
        pattern="^(follow|follow_request|like|comment|repost|mention)$",
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
