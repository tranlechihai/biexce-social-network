"""Social graph endpoints — friend requests, friendships, block/unblock.

All routes live under ``/api/social``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.models import Follow, FriendRequest, User
from ting_ting.schemas import (
    FollowRequestResponse,
    FriendRequestRequest,
    FriendRequestResponse,
    FriendTransitionRequest,
    RelationshipState,
    UserRef,
)
from ting_ting import social

router = APIRouter(prefix="/social", tags=["social"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _friend_request_response(db: Session, req: FriendRequest) -> FriendRequestResponse:
    sender = db.get(User, req.sender_id)
    recipient = db.get(User, req.recipient_id)
    return FriendRequestResponse(
        id=req.id,
        sender=_user_ref(sender) if sender else UserRef(id=req.sender_id, username="unknown"),
        recipient=_user_ref(recipient) if recipient else UserRef(id=req.recipient_id, username="unknown"),
        state=req.state,
        created_at=req.created_at.isoformat() if req.created_at else None,
    )


def _find_user(db: Session, target_id: int) -> User:
    """Look up a user by ID, returning 404 if not found."""
    user = db.get(User, target_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "User not found."},
        )
    return user


# ---------------------------------------------------------------------------
# Friend request endpoints
# ---------------------------------------------------------------------------

@router.post("/requests", response_model=FriendRequestResponse, status_code=status.HTTP_201_CREATED)
def send_friend_request(
    body: FriendRequestRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Send a friend request to another user.

    Raises 409 if a request, friendship, or block already exists for the pair.
    """
    target = _find_user(db, body.target_user_id)

    try:
        req = social.create_friend_request(db, me, target)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "self_request":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "Cannot send a friend request to yourself."},
            ) from None
        if reason == "already_exists":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "A friend request or friendship already exists."},
            ) from None
        if reason == "blocked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "A block prevents this action."},
            ) from None
        raise

    db.commit()
    db.refresh(req)
    return _friend_request_response(db, req)


@router.delete("/requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_sent_request_endpoint(
    request_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Cancel a sent, still-pending friend request (sender only)."""
    req = db.get(FriendRequest, request_id)
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Friend request not found."},
        )

    try:
        social.cancel_sent_request(db, req, me)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "not_sender":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the sender can cancel this request."},
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "This request is no longer pending."},
        ) from None

    db.commit()
    return None


@router.get("/requests")
def list_my_requests(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
    state: str | None = Query(None, pattern="^(pending|accepted|rejected)$"),
):
    """List friend requests sent to or received by the current user."""
    requests = social.list_requests(db, me.id, state_filter=state)
    db.commit()  # ensure stale refs are flushed
    return [_friend_request_response(db, r) for r in requests]


@router.post("/requests/accept")
def accept_request(
    body: FriendTransitionRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Accept an incoming friend request (recipient only)."""
    req = db.get(FriendRequest, body.request_id)
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Friend request not found."},
        )

    try:
        req = social.accept_friend_request(db, req, me)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "not_recipient":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the recipient can accept this request."},
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "This request is no longer pending."},
        ) from None

    db.commit()
    db.refresh(req)
    return _friend_request_response(db, req)


@router.post("/requests/reject")
def reject_request(
    body: FriendTransitionRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Reject an incoming friend request (recipient only)."""
    req = db.get(FriendRequest, body.request_id)
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Friend request not found."},
        )

    try:
        req = social.reject_friend_request(db, req, me)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "not_recipient":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the recipient can reject this request."},
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "This request is no longer pending."},
        ) from None

    db.commit()
    db.refresh(req)
    return _friend_request_response(db, req)


# ---------------------------------------------------------------------------
# Friendship endpoints
# ---------------------------------------------------------------------------

@router.get("/friends")
def list_my_friends(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """List the current user's active friends."""
    rows = social.list_friends(db, me.id)
    db.commit()
    results = []
    for row in rows:
        other_id = row.recipient_id if row.sender_id == me.id else row.sender_id
        other = db.get(User, other_id)
        if other:
            results.append(_user_ref(other))
    return results


@router.post("/friends/unfriend")
def unfriend_user(
    body: FriendRequestRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Remove an active friendship with another user."""
    target = _find_user(db, body.target_user_id)

    try:
        social.unfriend(db, me.id, target.id, me)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "not_participant":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Not a participant in this friendship."},
            ) from None
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "No active friendship with this user."},
        ) from None

    db.commit()
    return {"message": "Unfriended successfully."}


@router.get("/relationship/{target_user_id}", response_model=RelationshipState)
def get_relationship(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Get the current relationship state between me and another user."""
    _find_user(db, target_user_id)  # 404 if user doesn't exist

    state_code = social.relationship_state(db, me.id, target_user_id)

    detail_map = {
        "none": "No relationship.",
        "friends": "Friends.",
        "pending_outgoing": "Outgoing friend request pending.",
        "pending_incoming": "Incoming friend request pending.",
        "blocked_by_me": "You have blocked this user.",
        "blocked_by_them": "This user has blocked you.",
    }
    return RelationshipState(state=state_code, detail=detail_map.get(state_code))


# ---------------------------------------------------------------------------
# Block / unblock endpoints
# ---------------------------------------------------------------------------

@router.post("/blocks", status_code=status.HTTP_201_CREATED)
def block_target(
    body: FriendRequestRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Block another user (bilateral effect, removes friendship/requests)."""
    target = _find_user(db, body.target_user_id)

    try:
        blk = social.block_user(db, me, target)
    except ValueError as exc:
        if exc.args[0] == "self_block":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "Cannot block yourself."},
            ) from None
        if exc.args[0] == "already_blocked":
            # Lost a concurrent same-pair block race — the pair IS blocked.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "You have already blocked this user."},
            ) from None
        raise

    db.commit()
    return {"message": "User blocked.", "block_id": blk.id}


@router.delete("/blocks/{target_user_id}")
def unblock_target(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Unblock a user — does NOT restore any relationship."""
    _find_user(db, target_user_id)

    removed = social.unblock_user(db, me, target_user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "No active block found."},
        )

    db.commit()
    return {"message": "User unblocked."}


@router.get("/blocks")
def list_my_blocks(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """List users blocked by the current user."""
    rows = social.list_blocks(db, me.id)
    db.commit()
    results = []
    for blk in rows:
        blocked = db.get(User, blk.blocked_id)
        if blocked:
            results.append(_user_ref(blocked))
    return results


# ---------------------------------------------------------------------------
# Follow requests (T-024: private-account follow approval)
# ---------------------------------------------------------------------------

def _follow_request_response(db: Session, follow: Follow) -> FollowRequestResponse:
    requester = db.get(User, follow.follower_id)
    owner = db.get(User, follow.followed_id)
    return FollowRequestResponse(
        id=follow.id,
        requester=_user_ref(requester) if requester else UserRef(id=follow.follower_id, username="unknown"),
        owner=_user_ref(owner) if owner else UserRef(id=follow.followed_id, username="unknown"),
        status=follow.status,
        created_at=follow.created_at.isoformat() if follow.created_at else None,
    )


@router.get("/follow-requests", response_model=list[FollowRequestResponse])
def list_follow_requests(
    direction: str = Query("inbox", description="inbox | outgoing"),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """List pending follow requests (T-024).

    * ``direction=inbox`` — users waiting on MY approval;
    * ``direction=outgoing`` — users I requested follow (pending approval).
    """
    try:
        rows = social.list_follow_requests(db, me.id, direction)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": "direction must be 'inbox' or 'outgoing'."},
        ) from None
    return [_follow_request_response(db, r) for r in rows]


def _find_pending_follow(db: Session, request_id: int) -> Follow:
    follow = db.get(Follow, request_id)
    if follow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Follow request not found."},
        )
    if follow.status != "pending":
        # The edge was already decided (approved / rejected / cancelled) —
        # 409 keeps the client converging instead of acting on stale state.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "This follow request is no longer pending."},
        )
    return follow


@router.post("/follow-requests/{request_id}/approve", response_model=FollowRequestResponse, status_code=status.HTTP_200_OK)
def approve_follow_request_endpoint(
    request_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Approve a pending follow request (owner only) — the requester becomes
    an ACTIVE follower and receives a ``follow`` notification."""
    follow = _find_pending_follow(db, request_id)
    try:
        social.approve_follow_request(db, follow, me)
    except ValueError as exc:
        reason = exc.args[0]
        if reason == "not_owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the followed user can approve this request."},
            ) from None
        if reason == "blocked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "A block prevents this action."},
            ) from None
        raise
    db.commit()
    db.refresh(follow)
    return _follow_request_response(db, follow)


@router.post("/follow-requests/{request_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_follow_request_endpoint(
    request_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Reject a pending follow request (owner only) — the edge is deleted;
    the requester can follow again later."""
    follow = _find_pending_follow(db, request_id)
    try:
        social.reject_follow_request(db, follow, me)
    except ValueError as exc:
        if exc.args[0] == "not_owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Only the followed user can reject this request."},
            ) from None
        raise
    db.commit()
    return None
