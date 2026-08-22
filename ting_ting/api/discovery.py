"""Post discovery API — full-text search and exact hashtag timelines (T-026)."""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ting_ting.api.posts import _batch_post_responses
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import User
from ting_ting.schemas import (
    FEED_LIMIT_DEFAULT,
    FEED_LIMIT_MAX,
    FEED_LIMIT_MIN,
    PostResponse,
)
from ting_ting import search

router = APIRouter(tags=["discovery"])
NEXT_CURSOR_HEADER = "X-Next-Cursor"


@router.get("/search/posts", response_model=list[PostResponse])
def search_posts_api(
    response: Response,
    q: str = Query(min_length=1, max_length=search.SEARCH_QUERY_MAX),
    limit: int = Query(default=FEED_LIMIT_DEFAULT, ge=FEED_LIMIT_MIN, le=FEED_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Search visible post content using native FTS, newest first.

    Results use feed-like suppression (block/privacy/mute/deactivation) and a
    stable ``X-Next-Cursor``. Search operators are not accepted; input is
    converted to bounded plain lexical terms.
    """
    rows, next_cursor = search.query_post_search(db, me.id, q, limit, cursor)
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return _batch_post_responses(db, rows, me.id)


@router.get("/hashtags/{tag}/posts", response_model=list[PostResponse])
def hashtag_posts_api(
    response: Response,
    tag: str,
    limit: int = Query(default=FEED_LIMIT_DEFAULT, ge=FEED_LIMIT_MIN, le=FEED_LIMIT_MAX),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Exact normalized hashtag timeline with the same privacy contract."""
    rows, next_cursor = search.query_hashtag_posts(db, me.id, tag, limit, cursor)
    if next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return _batch_post_responses(db, rows, me.id)
