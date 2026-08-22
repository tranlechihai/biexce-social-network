"""Post-interaction REST API — likes and comments.

Endpoints (all require authentication + post visibility):

Likes:
* POST   /api/posts/{post_id}/likes         — like (idempotent)
* DELETE /api/posts/{post_id}/likes         — unlike (idempotent)

Comments:
* POST   /api/posts/{post_id}/comments       — create a comment
* GET    /api/posts/{post_id}/comments       — list comments (oldest-first)
* DELETE /api/posts/{post_id}/comments/{comment_id} — delete a comment

All interaction endpoints gate on *current* post visibility — a stored
interaction ID alone grants no access.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ting_ting.keyset import decode_cursor, encode_cursor

from ting_ting import notifications
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import Comment, Post, User
from ting_ting.schemas import (
    CommentCreateRequest,
    CommentResponse,
    CommentUpdateRequest,
    PostResponse,
    UserRef,
    FEED_LIMIT_MIN,
    FEED_LIMIT_MAX,
    FEED_LIMIT_DEFAULT,
    FEED_OFFSET_MIN,
)
from ting_ting import posts, interactions

router = APIRouter(prefix="/posts", tags=["interactions"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _post_response(db: Session, post: Post, viewer_id: int | None = None) -> PostResponse:
    """Build a PostResponse with current interaction summary."""
    author = db.get(User, post.author_id)
    from ting_ting.post_entities import post_entity_maps
    mentions, hashtags = post_entity_maps(db, [post.id])
    return PostResponse(
        id=post.id,
        author=_user_ref(author) if author else UserRef(id=post.author_id, username="unknown"),
        content=post.content,
        audience=post.audience,
        created_at=post.created_at.isoformat() if post.created_at else None,
        updated_at=post.updated_at.isoformat() if post.updated_at else None,
        like_count=interactions.count_likes(db, post.id),
        comment_count=interactions.count_comments(db, post.id),
        liked_by_viewer=bool(viewer_id and interactions.is_user_liked(db, viewer_id, post.id)),
        mentions=mentions.get(post.id, []),
        hashtags=hashtags.get(post.id, []),
    )


def _comment_response(db: Session, comment: Comment) -> CommentResponse:
    author = db.get(User, comment.author_id)
    return CommentResponse(
        id=comment.id,
        author=_user_ref(author) if author else UserRef(id=comment.author_id, username="unknown"),
        content=comment.content,
        created_at=comment.created_at.isoformat() if comment.created_at else None,
        parent_id=comment.parent_comment_id,
    )


def _find_post(db: Session, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Post not found."},
        )
    return post


def _require_post_visible(db: Session, post: Post, viewer_id: int) -> None:
    """Raise 404 if post is not visible to the viewer."""
    if not posts.is_visible_to(post.author_id, viewer_id, post.audience, db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Post not found."},
        )


# ---------------------------------------------------------------------------
# Like endpoints
# ---------------------------------------------------------------------------

@router.post("/{post_id}/likes", response_model=PostResponse)
def like_post(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Like a post — idempotent. Returns the updated PostResponse."""
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    interactions.create_like(db, me.id, post)
    notifications.record(db, post.author_id, me.id, "like", post.id)
    db.commit()
    db.refresh(post)
    return _post_response(db, post, viewer_id=me.id)


@router.delete("/{post_id}/likes", response_model=PostResponse)
def unlike_post(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Unlike a post — idempotent. Returns the updated PostResponse."""
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    interactions.remove_like(db, me.id, post)
    db.commit()
    db.refresh(post)
    return _post_response(db, post, viewer_id=me.id)


# ---------------------------------------------------------------------------
# Comment endpoints
# ---------------------------------------------------------------------------

@router.post("/{post_id}/comments", response_model=CommentResponse,
             status_code=status.HTTP_201_CREATED)
def create_comment_endpoint(
    post_id: int,
    body: CommentCreateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Create a comment (or one-level reply) on a visible post.

    A reply also notifies the parent comment's author in addition to the
    post author (deduplicated, self-safe).
    """
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    try:
        comment = interactions.create_comment(
            db, post, me.id, body.content,
            parent_comment_id=body.parent_comment_id,
        )
    except ValueError as exc:
        message_map = {
            "parent_not_found": "Parent comment not found.",
            "parent_mismatch": "Parent comment belongs to a different post.",
            "reply_to_reply": "Replies to replies are not allowed.",
        }
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": message_map.get(exc.args[0], "Invalid comment reply.")},
        ) from None

    recipient_ids = {post.author_id}
    if comment.parent_comment_id is not None:
        parent = db.get(Comment, comment.parent_comment_id)
        if parent is not None:
            recipient_ids.add(parent.author_id)
    for recipient_id in recipient_ids:
        notifications.record(db, recipient_id, me.id, "comment", post.id)
    db.commit()
    db.refresh(comment)
    return _comment_response(db, comment)


@router.patch("/{post_id}/comments/{comment_id}", response_model=CommentResponse)
def edit_comment_endpoint(
    post_id: int,
    comment_id: int,
    body: CommentUpdateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Edit a comment — the comment's author only."""
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    comment = db.get(Comment, comment_id)
    if comment is None or comment.post_id != post_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Comment not found."},
        )

    try:
        interactions.edit_comment(db, comment, me.id, body.content)
    except ValueError as exc:
        if exc.args[0] == "forbidden":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the comment author can edit this comment."},
            ) from None
        raise

    db.commit()
    db.refresh(comment)
    return _comment_response(db, comment)


NEXT_CURSOR_HEADER = "X-Next-Cursor"


@router.get("/{post_id}/comments", response_model=list[CommentResponse])
def list_comments_endpoint(
    response: Response,
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
    limit: int = Query(
        default=FEED_LIMIT_DEFAULT,
        ge=FEED_LIMIT_MIN,
        le=FEED_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
    offset: int = Query(default=FEED_OFFSET_MIN, ge=0, deprecated=True),
):
    """List comments on a visible post (oldest-first, keyset pagination).

    Pass the previous page's ``X-Next-Cursor`` header as ``cursor``.
    """
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    if cursor or offset == 0:
        # Keyset path (T-022): one bounded SQL page, oldest-first.
        after = None
        if cursor:
            try:
                after = decode_cursor(cursor)
            except ValueError:
                after = None  # malformed cursor → start over, never a 500
        rows = interactions.list_comments(db, post_id, limit=limit + 1, after=after)
        page = rows[:limit]
        if len(rows) > limit and page:
            response.headers[NEXT_CURSOR_HEADER] = encode_cursor(page[-1])
        return [_comment_response(db, c) for c in page]

    # Legacy offset walk (kept for old clients).
    all_comments = interactions.list_comments(db, post_id)[offset:]
    page = all_comments[:limit]
    if len(all_comments) > limit:
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(page[-1])
    return [_comment_response(db, c) for c in page]


@router.delete("/{post_id}/comments/{comment_id}")
def delete_comment_endpoint(
    post_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Delete a comment — comment author or post author only."""
    post = _find_post(db, post_id)
    _require_post_visible(db, post, me.id)

    comment = db.get(Comment, comment_id)
    if comment is None or comment.post_id != post_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Comment not found."},
        )

    try:
        interactions.delete_comment(db, comment, me.id, post)
    except ValueError as exc:
        if exc.args[0] == "forbidden":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "forbidden",
                    "message": "Only the comment author or post author can delete this comment.",
                },
            ) from None
        raise

    db.commit()
    return {"message": "Comment deleted."}
