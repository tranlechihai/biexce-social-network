"""Post & feed REST API — create, read, edit, delete, feed.

Endpoints:
* POST   /api/posts              — create a post
* GET    /api/posts/{post_id}     — read a post (visibility-checked)
* PATCH  /api/posts/{post_id}     — edit a post (author only)
* DELETE /api/posts/{post_id}     — delete a post (author only)
* GET    /api/feed                 — bounded visibility-filtered feed
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import User, Post, PostMedia
from ting_ting.media import delete_stored_file
from ting_ting.schemas import (
    PostCreateRequest,
    PostMediaResponse,
    PostResponse,
    PostUpdateRequest,
    UserRef,
    FEED_LIMIT_MIN,
    FEED_LIMIT_MAX,
    FEED_LIMIT_DEFAULT,
    FEED_OFFSET_MIN,
)
from ting_ting import posts, interactions

NEXT_CURSOR_HEADER = "X-Next-Cursor"

router = APIRouter(prefix="/posts", tags=["posts"])
feed_router = APIRouter(tags=["feed"])


def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _media_response(db: Session, post_id: int) -> list[PostMediaResponse]:
    rows = db.scalars(
        select(PostMedia).where(PostMedia.post_id == post_id).order_by(PostMedia.id)
    ).all()
    return [
        PostMediaResponse(
            id=m.id, post_id=post_id, url=m.path, media_type=m.media_type
        )
        for m in rows
    ]


def _post_response(db: Session, post: Post, viewer_id: int | None = None) -> PostResponse:
    """Build a PostResponse with current interaction summary."""
    author = db.get(User, post.author_id)
    liked, saved, reposted = posts.feed_viewer_states(db, viewer_id, [post.id])
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
        liked_by_viewer=post.id in liked,
        repost_count=interactions.count_reposts(db, post.id),
        saved_by_viewer=post.id in saved,
        reposted_by_viewer=post.id in reposted,
        media=_media_response(db, post.id),
        mentions=mentions.get(post.id, []),
        hashtags=hashtags.get(post.id, []),
    )


def _batch_post_responses(db: Session, feed_posts: list[Post],
                          viewer_id: int) -> list[PostResponse]:
    """Build PostResponses for a feed page with one grouped query per fact —
    no per-post round trips."""
    post_ids = [p.id for p in feed_posts]
    like_counts = posts.feed_like_counts(db, post_ids)
    comment_counts = posts.feed_comment_counts(db, post_ids)
    repost_counts = posts.feed_repost_counts(db, post_ids)
    liked, saved, reposted = posts.feed_viewer_states(db, viewer_id, post_ids)
    media_by_post = posts.feed_media(db, post_ids)
    authors, _profiles = posts.feed_authors(db, [p.author_id for p in feed_posts])
    from ting_ting.post_entities import post_entity_maps
    mentions, hashtags = post_entity_maps(db, post_ids)

    results = []
    for post in feed_posts:
        author = authors.get(post.author_id)
        results.append(PostResponse(
            id=post.id,
            author=_user_ref(author) if author else UserRef(id=post.author_id, username="unknown"),
            content=post.content,
            audience=post.audience,
            created_at=post.created_at.isoformat() if post.created_at else None,
            updated_at=post.updated_at.isoformat() if post.updated_at else None,
            like_count=like_counts.get(post.id, 0),
            comment_count=comment_counts.get(post.id, 0),
            liked_by_viewer=post.id in liked,
            repost_count=repost_counts.get(post.id, 0),
            saved_by_viewer=post.id in saved,
            reposted_by_viewer=post.id in reposted,
            media=[
                PostMediaResponse(
                    id=m.id, post_id=post.id,
                    url=m.path, media_type=m.media_type,
                )
                for m in media_by_post.get(post.id, [])
            ],
            mentions=mentions.get(post.id, []),
            hashtags=hashtags.get(post.id, []),
        ))
    return results


def _find_post(db: Session, post_id: int) -> Post:
    """Look up a post by ID; returns 404 if not found."""
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Post not found."},
        )
    return post


def _check_visible(db: Session, post: Post, viewer_id: int):
    """Raise 404 if the post is not visible to the viewer.

    We choose 404 (not_found) for hidden posts to avoid leaking existence
    information about the post or its author (non-leaking error contract).
    """
    if not posts.is_visible_to(post.author_id, viewer_id, post.audience, db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Post not found."},
        )


# ---------------------------------------------------------------------------
# Post CRUD endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post_endpoint(
    body: PostCreateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Create a new text post."""
    post = posts.create_post(db, me.id, body.content, body.audience)
    db.commit()
    db.refresh(post)
    return _post_response(db, post, viewer_id=me.id)


@router.get("/{post_id}", response_model=PostResponse)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Read a post — visibility checked at read time."""
    post = _find_post(db, post_id)
    _check_visible(db, post, me.id)
    return _post_response(db, post, viewer_id=me.id)


@router.patch("/{post_id}", response_model=PostResponse)
def edit_post_endpoint(
    post_id: int,
    body: PostUpdateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Edit a post — author only."""
    post = _find_post(db, post_id)

    try:
        post = posts.edit_post(
            db, post, me.id,
            content=body.content, audience=body.audience,
        )
    except ValueError as exc:
        if exc.args[0] == "forbidden":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the author can edit this post."},
            ) from None
        raise

    db.commit()
    db.refresh(post)
    return _post_response(db, post, viewer_id=me.id)


@router.delete("/{post_id}")
def delete_post_endpoint(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Delete a post — author only."""
    post = _find_post(db, post_id)

    media_paths = [
        row.path for row in db.query(PostMedia).filter(PostMedia.post_id == post.id).all()
    ]
    try:
        db.query(PostMedia).filter(PostMedia.post_id == post.id).delete()
        posts.delete_post(db, post, me.id)
    except ValueError as exc:
        if exc.args[0] == "forbidden":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the author can delete this post."},
            ) from None
        raise

    db.commit()
    for media_path in media_paths:
        delete_stored_file(media_path)
    return {"message": "Post deleted."}


# ---------------------------------------------------------------------------
# Feed endpoint
# ---------------------------------------------------------------------------

@feed_router.get("/feed", response_model=list[PostResponse])
def get_feed(
    response: Response,
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
    """Retrieve a bounded, visibility-filtered feed ordered newest-first.

    Keyset pagination: pass ``cursor`` from the previous page's
    ``X-Next-Cursor`` response header.  ``offset`` is kept for backwards
    compatibility (ignored when a valid cursor is supplied).
    """
    if cursor or offset == 0:
        # Cursor path — the first page (offset 0, no cursor) is the cursor
        # client's entry point and carries X-Next-Cursor.
        page, next_cursor = posts.query_feed(db, me.id, limit=limit, cursor=cursor)
    else:
        # Legacy offset walk over the visible slice (kept for old clients).
        full, _ = posts.query_feed(db, me.id, limit=limit + offset)
        page = full[offset:]
        next_cursor = None
    db.commit()
    items = _batch_post_responses(db, page, me.id)
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return items


@feed_router.get("/feed/following", response_model=list[PostResponse])
def get_following_feed(
    response: Response,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
    limit: int = Query(
        default=FEED_LIMIT_DEFAULT,
        ge=FEED_LIMIT_MIN,
        le=FEED_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None),
):
    """Following feed: posts + reposts of users the viewer follows.

    Visibility of each candidate is still applied at read time.
    """
    page, next_cursor = posts.query_following_feed(db, me.id, limit=limit, cursor=cursor)
    db.commit()
    items = _batch_post_responses(db, page, me.id)
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return items
