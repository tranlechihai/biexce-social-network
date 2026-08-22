"""Post & feed business logic — creation, current-authorization, feed ordering.

This module enforces audience policy at every read path. A post ID or prior
feed page grants no access on its own; the block/friend/follow graph and the
author's privacy flag are re-checked for each read.

Audience rules (checked at read time, privacy applies to public authors too):

* ``ONLY_ME``   — only the author.
* ``FRIENDS``   — the author and any user who is a *current, unblocked
  friend*.  A friendship is a mutual opt-in, so it is honored even when the
  author is private.
* ``PUBLIC``    — any authenticated, non-blocked user ... except when the
  author is **private** (T-024): then only friends and ACTIVE followers.
* ``FOLLOWERS`` — the author and ACTIVE followers (pending follow requests
  see nothing).

A private author's content is never reachable through ``PUBLIC``/
``FOLLOWERS`` by a non-following visitor; blocked pairs are denied for every
audience.
"""

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ting_ting.keyset import decode_cursor, encode_cursor
from ting_ting.post_entities import reconcile_post_entities
from ting_ting.models import (
    Block, Comment, FriendRequest, Like, Mute, Post,
    PostMedia, Repost, SavedPost, User, UserProfile,
)
from ting_ting import social


# ---------------------------------------------------------------------------
# Audience authorisation
# ---------------------------------------------------------------------------

def _is_friend(db: Session, user_a_id: int, user_b_id: int) -> bool:
    """True if the pair is currently accepted-friends (either direction)."""
    left, right = social.canonical_pair(user_a_id, user_b_id)
    return db.execute(
        select(FriendRequest.id).where(
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
            FriendRequest.state == "accepted",
        ).limit(1)
    ).scalar_one_or_none() is not None


def _private_author_gate(db: Session, author: "User", viewer_id: int) -> bool:
    """A private author's PUBLIC/FOLLOWERS-style reach: friends or ACTIVE
    followers only.  (Applied only for non-author viewers who are not
    blocked.)"""
    return _is_friend(db, author.id, viewer_id) or social.is_active_follower(
        db, viewer_id, author.id,
    )


def is_visible_to(author_id: int, viewer_id: int, audience: str, db: Session) -> bool:
    """Return ``True`` if the viewer may read a post with the given audience.

    Post access rules (re-evaluated on every read — see module docstring for
    the full matrix):
    * Author always sees their own post.
    * Suppressed authors (banned or self-deactivated) — denied for everyone
      else: their content leaves every feed, so a direct post id must not be
      a backdoor around feed suppression (404 keeps existence non-leaking).
    * ``ONLY_ME`` — author only.
    * ``FRIENDS`` — author + current mutual unblocked friends (friends stay
      friends even when the author is private).
    * ``PUBLIC`` — everyone, UNLESS the author is private (T-024): friends /
      active followers only.
    * ``FOLLOWERS`` — author + active followers; pending follow requests are
      denied (a request is not a subscription yet).
    * Any audience — blocked pair → denied.
    """
    # Author always sees own posts.
    if viewer_id == author_id:
        return True

    # Suppressed authors: banned (moderator) or self-deactivated (T-023).
    # A missing author is denied as well — fail closed.
    author = db.get(User, author_id)
    if (
        author is None
        or author.banned_at is not None
        or author.deactivated_at is not None
    ):
        return False

    # Blocked pair → denied regardless of audience.
    if social.is_blocked(db, author_id, viewer_id):
        return False

    if audience == "ONLY_ME":
        return False

    if audience == "FRIENDS":
        return _is_friend(db, author_id, viewer_id)

    if audience == "FOLLOWERS":
        return social.is_active_follower(db, viewer_id, author_id)

    if audience == "PUBLIC":
        if author.is_private:
            return _private_author_gate(db, author, viewer_id)
        return True

    # Unknown audience → deny by default.
    return False


# ---------------------------------------------------------------------------
# Post CRUD
# ---------------------------------------------------------------------------

def create_post(db: Session, author_id: int, content: str, audience: str) -> Post:
    """Create a new text post and persist it."""
    now = datetime.now(timezone.utc)
    post = Post(
        author_id=author_id,
        content=content,
        audience=audience,
        created_at=now,
        updated_at=now,
    )
    db.add(post)
    db.flush()
    new_mentions = reconcile_post_entities(db, post)
    if new_mentions:
        from ting_ting import notifications
        for target_id in new_mentions:
            if is_visible_to(post.author_id, target_id, post.audience, db):
                notifications.record(
                    db, target_id, post.author_id, "mention", post.id,
                    source_key=f"post:{post.id}",
                )
    return post


def edit_post(db: Session, post: Post, by_user_id: int,
              content: str | None = None, audience: str | None = None) -> Post:
    """Edit allowed fields — author only.

    Raises ``ValueError``: ``forbidden`` if not the author.
    """
    if post.author_id != by_user_id:
        raise ValueError("forbidden")

    content_changed = content is not None and content != post.content
    if content is not None:
        post.content = content
    if audience is not None:
        post.audience = audience
    post.updated_at = datetime.now(timezone.utc)
    db.flush()
    if content_changed:
        new_mentions = reconcile_post_entities(db, post)
        if new_mentions:
            from ting_ting import notifications
            for target_id in new_mentions:
                if is_visible_to(post.author_id, target_id, post.audience, db):
                    notifications.record(
                        db, target_id, post.author_id, "mention", post.id,
                        source_key=f"post:{post.id}",
                    )
    return post


def delete_post(db: Session, post: Post, by_user_id: int) -> Post:
    """Delete a post — author only.

    Raises ``ValueError``: ``forbidden`` if not the author.
    """
    if post.author_id != by_user_id:
        raise ValueError("forbidden")

    db.delete(post)
    db.flush()
    return post


# ---------------------------------------------------------------------------
# Feed — visibility filtered in SQL, keyset (cursor) pagination
# ---------------------------------------------------------------------------

def _blocked_ids(viewer_id: int):
    """Subquery: every user in a block pair with ``viewer_id`` (both
    directions)."""
    return (
        select(Block.blocked_id).where(Block.blocker_id == viewer_id)
        .union_all(select(Block.blocker_id).where(Block.blocked_id == viewer_id))
    )


def _friend_ids(viewer_id: int):
    """Subquery: every user currently accepted-friends with ``viewer_id``."""
    return (
        select(FriendRequest.recipient_id).where(
            FriendRequest.sender_id == viewer_id,
            FriendRequest.state == "accepted",
        )
        .union_all(
            select(FriendRequest.sender_id).where(
                FriendRequest.recipient_id == viewer_id,
                FriendRequest.state == "accepted",
            )
        )
    )


def _private_author_ids():
    """Subquery: private user ids (T-024) — their PUBLIC posts are only for
    friends / active followers, so the plain-PUBLIC branch must exclude them
    and a dedicated branch re-admits via the relationship."""
    return select(User.id).where(User.is_private.is_(True))


def _others_visible_condition(viewer_id: int):
    """Visibility for posts by *other* users (own posts are handled
    separately).  Mirrors ``is_visible_to`` exactly, including the T-024
    privacy gate: unknown audience values fall out of the OR — deny by
    default."""
    blocked = _blocked_ids(viewer_id)
    friends = _friend_ids(viewer_id)
    followed = social.active_followed_ids(viewer_id)
    private_authors = _private_author_ids()
    return or_(
        # PUBLIC by a non-private author: everyone (not blocked).
        and_(
            Post.audience == "PUBLIC",
            Post.author_id.not_in(private_authors),
            Post.author_id.not_in(blocked),
        ),
        # PUBLIC by a PRIVATE author: friends or active followers.
        and_(
            Post.audience == "PUBLIC",
            Post.author_id.in_(private_authors),
            or_(Post.author_id.in_(friends), Post.author_id.in_(followed)),
            Post.author_id.not_in(blocked),
        ),
        # FRIENDS: current accepted friends (friendship is a mutual opt-in —
        # privacy does not restrict it).
        and_(
            Post.audience == "FRIENDS",
            Post.author_id.in_(friends),
            Post.author_id.not_in(blocked),
        ),
        # FOLLOWERS: active followers only — pending requests see nothing.
        and_(
            Post.audience == "FOLLOWERS",
            Post.author_id.in_(followed),
            Post.author_id.not_in(blocked),
        ),
    )


def _muted_ids(viewer_id: int):
    """Subquery: users the viewer has muted (their posts leave the feeds)."""
    return select(Mute.target_id).where(
        Mute.muted_by == viewer_id, Mute.post_id.is_(None),
    )


def _muted_post_ids(viewer_id: int):
    """Subquery: posts the viewer has hidden from their feeds."""
    return select(Mute.post_id).where(
        Mute.muted_by == viewer_id, Mute.target_id.is_(None),
    )


def _banned_author_ids():
    """Subquery: banned user ids — their posts leave every viewer's feeds."""
    return select(User.id).where(User.banned_at.is_not(None))


def _deactivated_author_ids():
    """Subquery: self-deactivated user ids — posts leave every feed (T-023)."""
    return select(User.id).where(User.deactivated_at.is_not(None))


def _feed_suppression_conditions(viewer_id: int):
    return [
        Post.author_id.not_in(_muted_ids(viewer_id)),
        Post.author_id.not_in(_banned_author_ids()),
        Post.author_id.not_in(_deactivated_author_ids()),
        Post.id.not_in(_muted_post_ids(viewer_id)),
    ]


def _keyset_desc_condition(cursor: str):
    created_at, row_id = decode_cursor(cursor)
    return or_(
        Post.created_at < created_at,
        and_(Post.created_at == created_at, Post.id < row_id),
    )


def _paginate(desc_rows: list[Post], limit: int) -> tuple[list[Post], str | None]:
    has_more = len(desc_rows) > limit
    page = desc_rows[:limit]
    next_cursor = encode_cursor(page[-1]) if has_more else None
    return page, next_cursor


def query_feed(
    db: Session,
    viewer_id: int,
    limit: int | None = 20,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    """Return one page of the "for you" feed, newest first.

    Visibility is enforced *in SQL* (no load-all-then-filter) — see
    ``_others_visible_condition`` for the exact matrix (audience × privacy
    × block × friend × active-follower):
    * the viewer's own posts — any audience;
    * ``PUBLIC`` posts by non-blocked, non-private authors;
    * ``PUBLIC`` posts by private authors — friends / active followers;
    * ``FRIENDS`` posts by current accepted friends, not blocked;
    * ``FOLLOWERS`` posts by active followers, not blocked.

    Returns ``(posts, next_cursor)``; ``next_cursor`` is ``None`` on the last
    page.  A malformed cursor resets to the first page instead of failing.
    """
    stmt = select(Post).where(
        or_(Post.author_id == viewer_id, _others_visible_condition(viewer_id)),
        *_feed_suppression_conditions(viewer_id),
    )
    if cursor:
        try:
            stmt = stmt.where(_keyset_desc_condition(cursor))
        except ValueError:
            cursor = None

    rows = list(
        db.scalars(
            stmt.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
        ).all()
    )
    return _paginate(rows, limit)


def query_following_feed(
    db: Session,
    viewer_id: int,
    limit: int | None = 20,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    """One page of the following feed, newest first (by original post time).

    Feed candidates — visibility still applies to each:
    * posts by users the viewer ACTIVELY follows (a pending follow request
      grants no access while the author is still deciding);
    * reposts: the *original* posts of posts reposted by a user the viewer
      follows (a re-shared PUBLIC thread reaches the follower's feed).
    """
    followed = social.active_followed_ids(viewer_id)
    reposted_ids = select(Repost.post_id).where(Repost.user_id.in_(followed))
    visible = _others_visible_condition(viewer_id)

    stmt = select(Post).where(
        or_(
            and_(Post.author_id.in_(followed), visible),
            and_(Post.id.in_(reposted_ids), visible),
        ),
        *_feed_suppression_conditions(viewer_id),
    )
    if cursor:
        try:
            stmt = stmt.where(_keyset_desc_condition(cursor))
        except ValueError:
            cursor = None

    rows = list(
        db.scalars(
            stmt.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
        ).all()
    )
    return _paginate(rows, limit)


# ---------------------------------------------------------------------------
# Batched feed metadata — one grouped query per concern (no N+1)
# ---------------------------------------------------------------------------

def feed_like_counts(db: Session, post_ids: list[int]) -> dict[int, int]:
    if not post_ids:
        return {}
    rows = db.execute(
        select(Like.post_id, func.count(Like.id))
        .where(Like.post_id.in_(post_ids)).group_by(Like.post_id)
    ).all()
    return dict(rows)


def feed_comment_counts(db: Session, post_ids: list[int]) -> dict[int, int]:
    if not post_ids:
        return {}
    rows = db.execute(
        select(Comment.post_id, func.count(Comment.id))
        .where(Comment.post_id.in_(post_ids)).group_by(Comment.post_id)
    ).all()
    return dict(rows)


def feed_repost_counts(db: Session, post_ids: list[int]) -> dict[int, int]:
    if not post_ids:
        return {}
    rows = db.execute(
        select(Repost.post_id, func.count(Repost.id))
        .where(Repost.post_id.in_(post_ids)).group_by(Repost.post_id)
    ).all()
    return dict(rows)


def feed_viewer_states(
    db: Session, viewer_id: int, post_ids: list[int],
) -> tuple[set[int], set[int], set[int]]:
    """Return ``(liked, saved, reposted)`` post-id sets for the viewer."""
    liked: set[int] = set()
    saved: set[int] = set()
    reposted: set[int] = set()
    if not viewer_id or not post_ids:
        return liked, saved, reposted
    for model, target in (
        (Like, liked), (SavedPost, saved), (Repost, reposted),
    ):
        target.update(
            db.scalars(
                select(model.post_id).where(
                    model.user_id == viewer_id, model.post_id.in_(post_ids),
                )
            ).all()
        )
    return liked, saved, reposted


def feed_media(db: Session, post_ids: list[int]) -> dict[int, list[PostMedia]]:
    if not post_ids:
        return {}
    rows = db.scalars(
        select(PostMedia).where(PostMedia.post_id.in_(post_ids))
        .order_by(PostMedia.id.asc())
    ).all()
    grouped: dict[int, list[PostMedia]] = {}
    for media in rows:
        grouped.setdefault(media.post_id, []).append(media)
    return grouped


def feed_comments(db: Session, post_ids: list[int]) -> dict[int, list[Comment]]:
    if not post_ids:
        return {}
    rows = db.scalars(
        select(Comment).where(Comment.post_id.in_(post_ids))
        .order_by(Comment.created_at.asc(), Comment.id.asc())
    ).all()
    grouped: dict[int, list[Comment]] = {}
    for comment in rows:
        grouped.setdefault(comment.post_id, []).append(comment)
    return grouped


def feed_authors(
    db: Session, author_ids: list[int],
) -> tuple[dict[int, User | None], dict[int, UserProfile | None]]:
    users: dict[int, User | None] = {}
    profiles: dict[int, UserProfile | None] = {}
    for author_id in set(author_ids):
        users[author_id] = db.get(User, author_id)
        profiles[author_id] = db.get(UserProfile, author_id)
    return users, profiles


def list_saved_posts(
    db: Session,
    user_id: int,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    """One keyset page (newest saved first) of ``user_id``'s saved posts.

    Returns ``(posts, next_cursor)``; ``next_cursor`` is ``None`` on the
    last page.  Posts that no longer pass visibility checks (deleted,
    audience, block) are skipped — the page is filled with visible posts
    only, so the DB over-fetches a bounded amount to keep pages stable.
    """
    conditions = [SavedPost.user_id == user_id]
    if cursor:
        try:
            created_at, row_id = decode_cursor(cursor)
        except ValueError:
            return [], None  # malformed cursor -> start over, never a 500
        conditions.append(
            or_(
                SavedPost.created_at < created_at,
                and_(SavedPost.created_at == created_at, SavedPost.id < row_id),
            )
        )
    rows = db.scalars(
        select(SavedPost)
        .where(*conditions)
        .order_by(SavedPost.created_at.desc(), SavedPost.id.desc())
        .limit(limit * 2 + 4)
    ).all()

    posts: list[Post] = []
    skipped = 0
    last_row: SavedPost | None = None
    for row in rows:
        post = db.get(Post, row.post_id)
        if post is None or not is_visible_to(post.author_id, user_id, post.audience, db):
            skipped += 1
            continue
        posts.append(post)
        last_row = row
        if len(posts) == limit:
            consumed = limit + skipped
            next_cursor = encode_cursor(last_row) if consumed < len(rows) else None
            return posts, next_cursor
    return posts, None
