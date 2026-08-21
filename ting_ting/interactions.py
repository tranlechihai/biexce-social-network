"""Interaction business logic — idempotent likes and authorized comments.

Authorization for *every* interaction path follows existing post visibility
(rules in ``ting_ting.posts``).  A user who cannot read the post cannot
like, unlike, comment, list comments, or infer any interaction metadata.

Idempotency is guaranteed by database-level UNIQUE constraints:
* ``likes(user_id, post_id)`` — at most one like per user/post pair.
* A comment row is created at most once per request (application-level).
"""


from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import instance_state

from ting_ting.models import Comment, Like, Post, Repost


# ---------------------------------------------------------------------------
# Like helpers
# ---------------------------------------------------------------------------

class LikeNotFound(Exception):
    pass


class LikeNotOwned(Exception):
    pass


def create_like(db: Session, user_id: int, post: Post) -> Like:
    """Create or return the existing like for ``(user_id, post)``.

    Idempotent: if the row already exists, returns it without side-effect.
    Handles concurrent/retried inserts — ``IntegrityError`` escapes the
    nested savepoint block so the context manager rolls back *only* the
    savepoint; the outer transaction's prior work survives.  We then
    re-query and return the existing row (never a 500).
    """
    existing = db.execute(
        select(Like).where(
            Like.user_id == user_id,
            Like.post_id == post.id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        return existing

    like = None
    try:
        with db.begin_nested():
            like = Like(user_id=user_id, post_id=post.id)
            db.add(like)
            db.flush()
    except IntegrityError:
        # The ``with`` block's ``__exit__`` already rolled back the SAVEPOINT.
        # ``like`` is still pending in the session identity map (savepoint rollback
        # does NOT detach objects — they remain PENDING).  Expunge it so any
        # subsequent flush or commit does not re-attempt the INSERT on this row.
        if like is not None:
            st = instance_state(like)
            if st.session_id is not None:
                db.expunge(like)
        existing = db.execute(
            select(Like).where(
                Like.user_id == user_id,
                Like.post_id == post.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise  # unexpected: no row found after conflict
    return like


def remove_like(db: Session, user_id: int, post: Post) -> Like:
    """Remove the like for ``(user_id, post)``.

    Idempotent: if no row exists the function is a no-op (returns None).
    """
    existing = db.execute(
        select(Like).where(
            Like.user_id == user_id,
            Like.post_id == post.id,
        )
    ).scalar_one_or_none()

    if existing is None:
        return None

    db.delete(existing)
    db.flush()
    return existing


def count_likes(db: Session, post_id: int) -> int:
    """Return the number of ``Like`` rows for the given post."""
    return db.scalar(
        select(func.count(Like.id)).where(Like.post_id == post_id)
    )


def is_user_liked(db: Session, user_id: int, post_id: int) -> bool:
    """Return ``True`` if the user already liked the post."""
    return db.execute(
        select(Like).where(
            Like.user_id == user_id,
            Like.post_id == post_id,
        )
    ).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Comment helpers
# ---------------------------------------------------------------------------

def create_comment(
    db: Session,
    post: Post,
    author_id: int,
    content: str,
    parent_comment_id: int | None = None,
) -> Comment:
    """Create a comment (or one-level reply) on a visibility-checked post.

    Raises ``ValueError``:
    * ``parent_not_found`` — ``parent_comment_id`` does not exist;
    * ``parent_mismatch`` — the parent belongs to a different post;
    * ``reply_to_reply`` — the parent is itself a reply (max depth: 1).
    """
    parent_id = None
    if parent_comment_id is not None:
        parent = db.get(Comment, parent_comment_id)
        if parent is None:
            raise ValueError("parent_not_found")
        if parent.post_id != post.id:
            raise ValueError("parent_mismatch")
        if parent.parent_comment_id is not None:
            raise ValueError("reply_to_reply")
        parent_id = parent.id

    comment = Comment(
        post_id=post.id,
        author_id=author_id,
        content=content,
        parent_comment_id=parent_id,
    )
    db.add(comment)
    db.flush()
    return comment


def edit_comment(db: Session, comment: Comment, by_user_id: int, content: str) -> Comment:
    """Edit a comment — the comment's author only.

    Raises ``ValueError``: ``forbidden`` if not the author.
    """
    if comment.author_id != by_user_id:
        raise ValueError("forbidden")
    comment.content = content
    db.flush()
    return comment


def list_comments(
    db: Session,
    post_id: int,
    limit: int | None = None,
    after: tuple[datetime, int] | None = None,
) -> list[Comment]:
    """Return comments ordered oldest-first, then by id ascending.

    ``after`` is a keyset cursor ``(created_at, id)`` and ``limit`` caps how
    many rows are fetched starting past it (T-022 — callers that do not pass
    either still get the full ordered list, as the web view needs).
    """
    stmt = select(Comment).where(Comment.post_id == post_id)
    if after is not None:
        stmt = stmt.where(
            or_(
                Comment.created_at > after[0],
                and_(Comment.created_at == after[0], Comment.id > after[1]),
            )
        )
    stmt = stmt.order_by(Comment.created_at.asc(), Comment.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def count_comments(db: Session, post_id: int) -> int:
    """Return the number of ``Comment`` rows for the given post."""
    return db.scalar(
        select(func.count(Comment.id)).where(Comment.post_id == post_id)
    )


def count_reposts(db: Session, post_id: int) -> int:
    """Return the number of ``Repost`` rows for the given post."""
    return db.scalar(
        select(func.count(Repost.id)).where(Repost.post_id == post_id)
    )


def delete_comment(db: Session, comment: Comment, by_user_id: int, post: Post) -> Comment:
    """Delete a comment — only the comment author or post author may do so.

    Raises ``ValueError``: ``forbidden`` if caller is neither.
    """
    if by_user_id != comment.author_id and by_user_id != post.author_id:
        raise ValueError("forbidden")

    db.delete(comment)
    db.flush()
    return comment
