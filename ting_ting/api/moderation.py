"""Moderation REST API — reports, bans, and moderator content removal.

Endpoints (user-level):
* POST   /api/reports                    — file a report (post/comment/user)

Endpoints (moderator-level, 403 for non-moderators):
* GET    /api/reports                     — report queue (status filter)
* POST   /api/reports/{id}/resolve        — resolve (action taken)
* POST   /api/reports/{id}/dismiss        — dismiss (no action)
* POST   /api/social/bans                 — ban a user
* DELETE /api/social/bans/{user_id}       — unban
* DELETE /api/mod/posts/{post_id}         — remove a post
* DELETE /api/mod/comments/{comment_id}   — remove a comment (+ replies)
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting import moderation
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.media import delete_stored_file
from ting_ting.models import Comment, Post, PostMedia, Report, User
from ting_ting.posts import is_visible_to
from ting_ting.schemas import ReportCreateRequest, ReportResponse

router = APIRouter(tags=["moderation"])


def get_moderator_role(
    me: User = Depends(get_current_user),
) -> User:
    """Dependency: require the authenticated user to be a moderator."""
    if not me.is_moderator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "Moderator privileges are required.",
            },
        )
    return me


def _report_response(db: Session, report: Report) -> ReportResponse:
    def _ref(user_id: int | None) -> dict | None:
        if user_id is None:
            return None
        user = db.get(User, user_id)
        if user is None:
            return {"id": user_id, "username": "unknown", "display_name": None}
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
        }

    return ReportResponse(
        id=report.id,
        reporter=_ref(report.reporter_id),
        target_user=_ref(report.target_user_id),
        post_id=report.post_id,
        comment_id=report.comment_id,
        reason=report.reason,
        status=report.status,
        resolution_note=report.resolution_note,
        resolved_by=_ref(report.resolved_by) if report.resolved_by else None,
        created_at=report.created_at.isoformat() if report.created_at else None,
        resolved_at=report.resolved_at.isoformat() if report.resolved_at else None,
    )


def _find_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "User not found."},
        )
    return user


def _find_post(db: Session, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Post not found."},
        )
    return post


# ---------------------------------------------------------------------------
# Reports — file (any authenticated user)
# ---------------------------------------------------------------------------

@router.post(
    "/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_report(
    body: ReportCreateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Report a user's post, comment, or bare account.

    Visibility is enforced: you can only report content you can currently see.
    Duplicate reports for the same (target, content) return the existing row.
    """
    target = _find_user(db, body.target_user_id)

    # Pin the reported content. A comment report is anchored to the
    # comment's own post and its author.
    post_id = body.post_id
    comment_id = body.comment_id
    if comment_id is not None:
        comment = db.get(Comment, comment_id)
        if comment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "Comment not found."},
            )
        post_id = comment.post_id
        if body.target_user_id != comment.author_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "validation",
                    "message": "The report target must be the comment author.",
                },
            )

    # Visibility is enforced: content you cannot see cannot be reported
    # (no existence leaks through the report surface).
    if post_id is not None:
        post = _find_post(db, post_id)
        if not is_visible_to(post.author_id, me.id, post.audience, db):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "Post not found."},
            )

    try:
        report = moderation.create_report(
            db,
            me,
            target.id,
            reason=body.reason,
            post_id=post_id,
            comment_id=comment_id,
        )
    except ValueError as exc:
        message_map = {
            "invalid_reason": "Invalid report reason.",
            "self_report": "You cannot report your own account.",
            "content_requires_post": "A comment report requires its post.",
        }
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation",
                "message": message_map.get(exc.args[0], "Invalid report."),
            },
        ) from None

    db.commit()
    db.refresh(report)
    return _report_response(db, report)


# ---------------------------------------------------------------------------
# Reports — moderator queue + resolution
# ---------------------------------------------------------------------------

@router.get("/reports", response_model=list[ReportResponse])
def list_reports(
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
    status_: str | None = Query(default=None, alias="status",
                               pattern="^(pending|resolved|dismissed)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """The moderation queue — moderators only."""
    rows = moderation.list_reports(db, status_filter=status_,
                                   limit=limit, offset=offset)
    db.commit()
    return [_report_response(db, r) for r in rows]


def _mod_resolve(db: Session, me: User, report_id: int, dismiss: bool,
                 note: str | None):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Report not found."},
        )
    try:
        moderation.resolve_report(db, me, report, dismiss=dismiss, note=note)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": "This report has already been processed.",
            },
        ) from None
    db.commit()
    db.refresh(report)
    return _report_response(db, report)


@router.post("/reports/{report_id}/resolve", response_model=ReportResponse)
def resolve_report(
    report_id: int,
    note: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Resolve a report (an enforcement action was taken)."""
    return _mod_resolve(db, me, report_id, dismiss=False, note=note)


@router.post("/reports/{report_id}/dismiss", response_model=ReportResponse)
def dismiss_report(
    report_id: int,
    note: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Dismiss a report (no enforcement action)."""
    return _mod_resolve(db, me, report_id, dismiss=True, note=note)


# ---------------------------------------------------------------------------
# Ban / unban
# ---------------------------------------------------------------------------

@router.post(
    "/social/bans",
    status_code=status.HTTP_200_OK,
)
def ban_user_endpoint(
    body: dict,
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Ban a user — freezes the account, severs relationships, resolves
    their pending reports. Idempotent."""
    raw_id = body.get("user_id") if isinstance(body, dict) else None
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": "user_id is required."},
        )
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": "user_id must be an integer."},
        ) from None
    target = _find_user(db, target_id)

    try:
        moderation.ban_user(db, me, target)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": "Cannot ban yourself.",
            },
        ) from None

    db.commit()
    return {"message": "User banned.", "user_id": target.id}


@router.delete("/social/bans/{user_id}")
def unban_user_endpoint(
    user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Lift a ban — idempotent, does not restore severed relationships."""
    target = _find_user(db, user_id)

    try:
        moderation.unban_user(db, me, target)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": "Cannot unban yourself.",
            },
        ) from None

    db.commit()
    return {"message": "User unbanned."}


# ---------------------------------------------------------------------------
# Moderator content removal
# ---------------------------------------------------------------------------

@router.delete("/mod/posts/{post_id}")
def mod_delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Remove a post as a moderation action (author check bypassed).

    Media files are cleaned up too (T-022) — same convention as the
    author delete path: collect paths, commit the row deletion, then
    unlink so a failed commit never leaves missing files.
    """
    post = _find_post(db, post_id)
    media_paths = list(
        db.scalars(select(PostMedia.path).where(PostMedia.post_id == post.id)).all()
    )
    moderation.delete_post_moderation(db, post)
    db.commit()
    for path in media_paths:
        delete_stored_file(path)
    return {"message": "Post removed."}


@router.delete("/mod/comments/{comment_id}")
def mod_delete_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_moderator_role),
):
    """Remove a comment (replies cascade) as a moderation action."""
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Comment not found."},
        )
    moderation.delete_comment_moderation(db, comment)
    db.commit()
    return {"message": "Comment removed."}
