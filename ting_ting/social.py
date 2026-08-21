"""Social graph business logic — canonical pairs, relationship queries, and transitions.

This module is the single source of truth for friendship and blocking rules.
All API routes delegate here.  Later tasks (posts, feed, interactions) should
call the authorization helpers in this module rather than duplicating logic.
"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting.models import Block, Follow, FriendRequest, Mute, User


# ---------------------------------------------------------------------------
# Canonical pair helpers
# ---------------------------------------------------------------------------

def canonical_pair(id_a: int, id_b: int) -> tuple[int, int]:
    """Return the ordered (min, max) representation of an unordered pair."""
    return (min(id_a, id_b), max(id_a, id_b))


# ---------------------------------------------------------------------------
# Block checks (bilateral)
# ---------------------------------------------------------------------------

def is_blocked(db: Session, user_a_id: int, user_b_id: int) -> bool:
    """Return True if *either* user has blocked the *other*."""
    stmt = select(Block).where(
        (
            (Block.blocker_id == user_a_id) & (Block.blocked_id == user_b_id)
        ) | (
            (Block.blocker_id == user_b_id) & (Block.blocked_id == user_a_id)
        )
    )
    return db.scalar(stmt) is not None


# ---------------------------------------------------------------------------
# Relationship state
# ---------------------------------------------------------------------------

def relationship_state(db: Session, viewer_id: int, target_id: int) -> str:
    """Return the current relationship as seen by ``viewer`` toward ``target``.

    States: blocked_by_me | blocked_by_them | friends |
            pending_outgoing | pending_incoming | none
    """
    # 1) Block check
    if db.scalar(select(Block).where(
        Block.blocker_id == viewer_id, Block.blocked_id == target_id,
    )) is not None:
        return "blocked_by_me"
    if db.scalar(select(Block).where(
        Block.blocker_id == target_id, Block.blocked_id == viewer_id,
    )) is not None:
        return "blocked_by_them"

    # 2) Active request / friendship
    left, right = canonical_pair(viewer_id, target_id)
    active = db.execute(
        select(FriendRequest).where(
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
            FriendRequest.state.in_(["pending", "accepted"]),
        )
    ).scalar_one_or_none()

    if active is None:
        return "none"
    if active.state == "accepted":
        return "friends"
    # pending
    if active.sender_id == viewer_id:
        return "pending_outgoing"
    return "pending_incoming"


# ---------------------------------------------------------------------------
# Friend request creation (AC1, AC2)
# ---------------------------------------------------------------------------

def create_friend_request(
    db: Session,
    sender: User,
    recipient: User,
) -> FriendRequest:
    """Create a pending friend request.

    Raises ``ValueError``: ``self_request``, ``already_exists``, ``blocked``.
    """
    if sender.id == recipient.id:
        raise ValueError("self_request")

    if is_blocked(db, sender.id, recipient.id):
        raise ValueError("blocked")

    left, right = canonical_pair(sender.id, recipient.id)

    existing = db.execute(
        select(FriendRequest).where(
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
            FriendRequest.state.in_(["pending", "accepted"]),
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise ValueError("already_exists")

    req = FriendRequest(
        sender_id=sender.id,
        recipient_id=recipient.id,
        canonical_left=left,
        canonical_right=right,
        state="pending",
    )
    db.add(req)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent same-pair request won the unique-pair race: the check
        # above passed before the other writer committed. Converge to the
        # same 409 the sequential path produces instead of a 500.
        db.rollback()
        raise ValueError("already_exists") from None
    return req


# ---------------------------------------------------------------------------
# Friend request transitions (AC3, AC4)
# ---------------------------------------------------------------------------

def accept_friend_request(
    db: Session,
    request: FriendRequest,
    by_user: User,
) -> FriendRequest:
    """Accept a pending request — must be the recipient.

    Raises ``ValueError``: ``not_recipient``, ``not_pending``.
    """
    if request.recipient_id != by_user.id:
        raise ValueError("not_recipient")
    if request.state != "pending":
        raise ValueError("not_pending")
    # Recheck the block graph at transition time: a sender may have blocked
    # the recipient (or vice versa) after sending the request.
    if is_blocked(db, request.sender_id, request.recipient_id):
        raise ValueError("blocked")

    request.state = "accepted"
    db.flush()
    return request


def reject_friend_request(
    db: Session,
    request: FriendRequest,
    by_user: User,
) -> FriendRequest:
    """Reject a pending request — must be the recipient.

    Raises ``ValueError``: ``not_recipient``, ``not_pending``.
    """
    if request.recipient_id != by_user.id:
        raise ValueError("not_recipient")
    if request.state != "pending":
        raise ValueError("not_pending")

    # Remove stale rejected rows for the same canonical pair before
    # transitioning this row.  Under the original uq_pair_state constraint
    # (blanket on all states) two rows with identical (L, R, "rejected")
    # are not allowed.  Under the partial ix_active_pair index it is
    # harmless — this cleanup is the runtime-side compatibility shim that
    # avoids any schema migration.
    db.execute(
        FriendRequest.__table__.delete().where(
            FriendRequest.id != request.id,
            FriendRequest.canonical_left == request.canonical_left,
            FriendRequest.canonical_right == request.canonical_right,
            FriendRequest.state == "rejected",
        )
    )

    request.state = "rejected"
    db.flush()
    return request


def unfriend(
    db: Session,
    user_a_id: int,
    user_b_id: int,
    by_user: User,
) -> list[FriendRequest]:
    """Sever an active friendship between two users.

    Both parties can initiate; either party must be ``by_user``.
    Raises ``ValueError``: ``not_participant``, ``not_friends``.
    """
    if by_user.id not in (user_a_id, user_b_id):
        raise ValueError("not_participant")

    left, right = canonical_pair(user_a_id, user_b_id)

    active = db.execute(
        select(FriendRequest).where(
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
            FriendRequest.state == "accepted",
        )
    ).scalar_one_or_none()

    if active is None:
        raise ValueError("not_friends")

    # Remove stale rejected rows for the same pair before marking this
    # friendship as rejected.  Same rationale as reject_friend_request.
    db.execute(
        FriendRequest.__table__.delete().where(
            FriendRequest.id != active.id,
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
            FriendRequest.state == "rejected",
        )
    )

    active.state = "rejected"
    db.flush()
    return [active]


# ---------------------------------------------------------------------------
# Listing helpers
# ---------------------------------------------------------------------------

def list_requests(
    db: Session,
    user_id: int,
    state_filter: str | None = None,
) -> list[FriendRequest]:
    """List friend requests relevant to the given user (sent or received)."""
    conditions = or_(
        FriendRequest.sender_id == user_id,
        FriendRequest.recipient_id == user_id,
    )
    if state_filter:
        conditions = and_(conditions, FriendRequest.state == state_filter)

    stmt = (
        select(FriendRequest)
        .where(conditions)
        .order_by(FriendRequest.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def list_friends(db: Session, user_id: int) -> list[FriendRequest]:
    """List active friendships for ``user_id`` (accepted state)."""
    stmt = (
        select(FriendRequest)
        .where(
            or_(
                FriendRequest.sender_id == user_id,
                FriendRequest.recipient_id == user_id,
            ),
            FriendRequest.state == "accepted",
        )
    )
    return list(db.execute(stmt).scalars().all())


def list_blocks(db: Session, user_id: int) -> list[Block]:
    """List blocks initiated by ``user_id`` (outgoing blocks)."""
    stmt = select(Block).where(Block.blocker_id == user_id)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Block / unblock (AC5, AC6)
# ---------------------------------------------------------------------------

def block_user(
    db: Session,
    blocker: User,
    blocked: User,
) -> Block:
    """Block another user — bilateral effect.

    Removes any active friendship / pending request and creates a ``Block`` row.
    Raises ``ValueError``: ``self_block``.
    """
    if blocker.id == blocked.id:
        raise ValueError("self_block")

    left, right = canonical_pair(blocker.id, blocked.id)

    # Remove all request rows for this pair (pending, accepted, rejected)
    for req in db.execute(
        select(FriendRequest).where(
            FriendRequest.canonical_left == left,
            FriendRequest.canonical_right == right,
        )
    ).scalars().all():
        db.delete(req)

    for follow in db.execute(
        select(Follow).where(
            ((Follow.follower_id == blocker.id) & (Follow.followed_id == blocked.id))
            | ((Follow.follower_id == blocked.id) & (Follow.followed_id == blocker.id))
        )
    ).scalars().all():
        db.delete(follow)

    # Create block (idempotent via unique constraint)
    existing = db.execute(
        select(Block).where(
            Block.blocker_id == blocker.id,
            Block.blocked_id == blocked.id,
        )
    ).scalar_one_or_none()

    if existing is None:
        blk = Block(blocker_id=blocker.id, blocked_id=blocked.id)
        db.add(blk)
        try:
            db.flush()
        except IntegrityError:
            # Concurrent block won the unique-pair race. Roll back the
            # relationship severing too (one flush covered it) — the other
            # writer's transaction already did the same thing.
            db.rollback()
            raise ValueError("already_blocked") from None
        return blk
    return existing


def unblock_user(
    db: Session,
    unblocker: User,
    target_id: int,
) -> bool:
    """Remove a block the user initiated toward ``target_id``.

    Does NOT restore any relationship. Only the original blocker may unblock.
    Returns True if a block was removed.
    """
    blk = db.execute(
        select(Block).where(
            Block.blocker_id == unblocker.id,
            Block.blocked_id == target_id,
        )
    ).scalar_one_or_none()

    if blk is None:
        return False

    db.delete(blk)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Cancel sent friend request
# ---------------------------------------------------------------------------

def cancel_sent_request(
    db: Session,
    request: FriendRequest,
    by_user: User,
) -> FriendRequest:
    """Delete a still-pending request — only its *sender* may cancel.

    Raises ``ValueError``: ``not_sender``, ``not_pending``.
    """
    if request.sender_id != by_user.id:
        raise ValueError("not_sender")
    if request.state != "pending":
        raise ValueError("not_pending")

    db.delete(request)
    db.flush()
    return request


# ---------------------------------------------------------------------------
# Mutes (unilateral, no relationship effect)
# ---------------------------------------------------------------------------

def mute_user(
    db: Session,
    muter: User,
    target_id: int,
) -> Mute:
    """Mute a user — idempotent (returns the existing row).

    Raises ``ValueError``: ``self_mute``.
    """
    if target_id == muter.id:
        raise ValueError("self_mute")

    existing = db.execute(
        select(Mute).where(
            Mute.muted_by == muter.id,
            Mute.target_id == target_id,
            Mute.post_id.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    mute = Mute(muted_by=muter.id, target_id=target_id)
    db.add(mute)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError("already_muted") from None
    return mute


def unmute_user(db: Session, muter: User, target_id: int) -> bool:
    """Remove a user mute. Returns True if a mute was removed."""
    mute = db.execute(
        select(Mute).where(
            Mute.muted_by == muter.id,
            Mute.target_id == target_id,
            Mute.post_id.is_(None),
        )
    ).scalar_one_or_none()
    if mute is None:
        return False
    db.delete(mute)
    db.flush()
    return True


def mute_post(db: Session, muter: User, post_id: int) -> Mute:
    """Hide a single post from the muter's feeds — idempotent."""
    existing = db.execute(
        select(Mute).where(
            Mute.muted_by == muter.id,
            Mute.post_id == post_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    mute = Mute(muted_by=muter.id, post_id=post_id)
    db.add(mute)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError("already_muted") from None
    return mute


def unmute_post(db: Session, muter: User, post_id: int) -> bool:
    """Remove a post mute. Returns True if a mute was removed."""
    mute = db.execute(
        select(Mute).where(
            Mute.muted_by == muter.id,
            Mute.post_id == post_id,
        )
    ).scalar_one_or_none()
    if mute is None:
        return False
    db.delete(mute)
    db.flush()
    return True


def is_muted_by(db: Session, viewer_id: int, target_id: int) -> bool:
    """Return True if ``viewer_id`` has muted user ``target_id`` (directional)."""
    return db.scalar(
        select(Mute).where(
            Mute.muted_by == viewer_id,
            Mute.target_id == target_id,
            Mute.post_id.is_(None),
        )
    ) is not None


# ---------------------------------------------------------------------------
# User search (keyset over username/id ascending)
# ---------------------------------------------------------------------------

def search_users(
    db: Session,
    viewer_id: int,
    query: str | None,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[User], str | None]:
    """Search users by username/display_name substring, excluding the viewer
    and users who blocked the viewer (parity with the web people page).

    Returns ``(page, next_cursor)``; a malformed cursor resets to the first
    page instead of failing.
    """
    from ting_ting.keyset import decode_pair, encode_pair

    conditions = [
        User.id != viewer_id,
        User.id.not_in(
            select(Block.blocker_id).where(Block.blocked_id == viewer_id)
        ),
        User.banned_at.is_(None),
        User.deactivated_at.is_(None),  # T-023: self-deactivated users hide
    ]

    text = (query or "").strip().lower()
    if text:
        like = f"%{text}%"
        conditions.append(
            or_(User.username.ilike(like), User.display_name.ilike(like))
        )

    if cursor:
        try:
            key, row_id = decode_pair(cursor)
            conditions.append(
                or_(
                    User.username > key,
                    and_(User.username == key, User.id > row_id),
                )
            )
        except ValueError:
            cursor = None  # malformed → start over, never a 500

    rows = list(
        db.scalars(
            select(User).where(*conditions)
            .order_by(User.username.asc(), User.id.asc())
            .limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_pair(page[-1].username, page[-1].id) if has_more else None
    return page, next_cursor


# ---------------------------------------------------------------------------
# Follow listing helpers
# ---------------------------------------------------------------------------

def user_counts(db: Session, user_id: int) -> dict[str, int]:
    """Return follower/following/friend counts for ``user_id``."""
    followers = db.scalar(
        select(func.count(Follow.id)).where(Follow.followed_id == user_id)
    ) or 0
    following = db.scalar(
        select(func.count(Follow.id)).where(Follow.follower_id == user_id)
    ) or 0
    friends = db.scalar(
        select(func.count(FriendRequest.id)).where(
            (
                (FriendRequest.sender_id == user_id)
                | (FriendRequest.recipient_id == user_id)
            ),
            FriendRequest.state == "accepted",
        )
    ) or 0
    return {"followers": followers, "following": following, "friends": friends}
