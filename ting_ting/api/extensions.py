"""REST API parity for profile, follows, activity, saved/repost, and media."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting import notifications, social
from ting_ting.api.posts import _post_response
from ting_ting.auth import get_current_user
from ting_ting.database import get_db
from ting_ting.media import delete_stored_file, store_post_upload
from ting_ting.models import (
    Activity, Follow, Mute, Post, PostMedia, Repost, SavedPost, User,
    UserProfile,
)
from ting_ting.posts import is_visible_to
from ting_ting.schemas import (
    ActivityResponse, ExtendedProfileResponse, ExtendedProfileUpdateRequest,
    PostMediaResponse, PostResponse, ToggleResponse, UserRef,
)


profile_router = APIRouter(prefix="/profile", tags=["profile"])
social_router = APIRouter(prefix="/social", tags=["social"])
activity_router = APIRouter(prefix="/activity", tags=["activity"])
feature_router = APIRouter(tags=["post-features"])


def _user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, username=user.username, display_name=user.display_name)


def _profile_response(user: User, profile: UserProfile | None) -> ExtendedProfileResponse:
    return ExtendedProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        bio=user.bio,
        birthday=profile.birthday if profile else None,
        gender=profile.gender if profile else None,
        location=profile.location if profile else None,
        occupation=profile.occupation if profile else None,
        website=profile.website if profile else None,
        avatar_url=(profile.avatar_path or profile.avatar_url) if profile else None,
    )


def _find_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "User not found."})
    return user


def _visible_post(db: Session, post_id: int, viewer_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None or not is_visible_to(post.author_id, viewer_id, post.audience, db):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Post not found."})
    return post


@profile_router.get("/me/details", response_model=ExtendedProfileResponse)
def get_profile_details(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    return _profile_response(me, db.get(UserProfile, me.id))


@profile_router.patch("/me/details", response_model=ExtendedProfileResponse)
def update_profile_details(
    body: ExtendedProfileUpdateRequest,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    profile = db.get(UserProfile, me.id) or UserProfile(user_id=me.id)
    db.add(profile)
    values = body.model_dump(exclude_unset=True)
    for field in ("display_name", "bio"):
        if field in values:
            setattr(me, field, values.pop(field))
    for field, value in values.items():
        setattr(profile, field, value)
    db.commit()
    return _profile_response(me, profile)


@social_router.put("/follows/{target_user_id}", response_model=ToggleResponse)
def follow_user(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    target = _find_user(db, target_user_id)
    if target.id == me.id:
        raise HTTPException(status_code=409, detail={"code": "conflict", "message": "Cannot follow yourself."})
    if social.is_blocked(db, me.id, target.id):
        raise HTTPException(status_code=409, detail={"code": "conflict", "message": "A block prevents this action."})
    existing = db.scalar(select(Follow).where(
        Follow.follower_id == me.id, Follow.followed_id == target.id,
    ))
    if existing is None:
        follow = None
        try:
            with db.begin_nested():
                follow = Follow(follower_id=me.id, followed_id=target.id)
                db.add(follow)
                notifications.record(db, target.id, me.id, "follow")
                db.flush()
        except IntegrityError:
            # A concurrent request inserted the same follow first — the
            # savepoint already rolled back our rows, so converge instead of
            # failing.
            from sqlalchemy.orm.attributes import instance_state
            if follow is not None and instance_state(follow).session_id is not None:
                db.expunge(follow)
            existing = db.scalar(select(Follow).where(
                Follow.follower_id == me.id, Follow.followed_id == target.id,
            ))
            if existing is None:
                raise
    return ToggleResponse(active=True)


@social_router.delete("/follows/{target_user_id}", response_model=ToggleResponse)
def unfollow_user(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    _find_user(db, target_user_id)
    existing = db.scalar(select(Follow).where(
        Follow.follower_id == me.id, Follow.followed_id == target_user_id,
    ))
    if existing is not None:
        db.delete(existing)
        db.commit()
    return ToggleResponse(active=False)


def _follow_users(db: Session, query, me: User, other_column) -> list[UserRef]:
    rows = db.scalars(query).all()
    users = []
    for row in rows:
        user = db.get(User, getattr(row, other_column))
        if user is not None and not social.is_blocked(db, me.id, user.id):
            users.append(_user_ref(user))
    return users


@social_router.put("/mutes/{target_user_id}", response_model=ToggleResponse)
def mute_user_endpoint(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Mute a user — hides their posts from your feeds and their
    notifications from you.  Does not affect any relationship."""
    target = _find_user(db, target_user_id)

    try:
        social.mute_user(db, me, target.id)
    except ValueError as exc:
        if exc.args[0] == "self_mute":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "conflict", "message": "Cannot mute yourself."},
            ) from None
        raise

    db.commit()
    return ToggleResponse(active=True)


@social_router.delete("/mutes/{target_user_id}", response_model=ToggleResponse)
def unmute_user_endpoint(
    target_user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Remove a mute — idempotent."""
    _find_user(db, target_user_id)
    removed = social.unmute_user(db, me, target_user_id)
    db.commit()
    return ToggleResponse(active=not removed)


@social_router.get("/following", response_model=list[UserRef])
def list_following(db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _follow_users(
        db,
        select(Follow).where(Follow.follower_id == me.id).order_by(Follow.created_at.desc(), Follow.id.desc()),
        me,
        "followed_id",
    )


@social_router.get("/followers", response_model=list[UserRef])
def list_followers(db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _follow_users(
        db,
        select(Follow).where(Follow.followed_id == me.id).order_by(Follow.created_at.desc(), Follow.id.desc()),
        me,
        "follower_id",
    )


@activity_router.get("", response_model=list[ActivityResponse])
def list_activity(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rows = db.scalars(
        select(Activity).where(Activity.user_id == me.id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all()
    visible = []
    for row in rows:
        actor = db.get(User, row.actor_id)
        if actor is not None and not social.is_blocked(db, me.id, actor.id):
            visible.append(ActivityResponse(
                id=row.id,
                actor=_user_ref(actor),
                kind=row.kind,
                post_id=row.post_id,
                created_at=row.created_at.isoformat() if row.created_at else None,
            ))
    return visible[offset:offset + limit]


@feature_router.get("/saved", response_model=list[PostResponse])
def list_saved_posts(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rows = db.scalars(
        select(SavedPost).where(SavedPost.user_id == me.id)
        .order_by(SavedPost.created_at.desc(), SavedPost.id.desc())
    ).all()
    posts = []
    for row in rows:
        post = db.get(Post, row.post_id)
        if post is not None and is_visible_to(post.author_id, me.id, post.audience, db):
            posts.append(_post_response(db, post, viewer_id=me.id))
    return posts[offset:offset + limit]


def _toggle_post_feature(db: Session, model, me: User, post_id: int, active: bool):
    post = _visible_post(db, post_id, me.id)
    row = db.scalar(select(model).where(model.user_id == me.id, model.post_id == post.id))
    if active and row is None:
        new_row = None
        try:
            with db.begin_nested():
                new_row = model(user_id=me.id, post_id=post.id)
                db.add(new_row)
                if model is Repost:
                    notifications.record(db, post.author_id, me.id, "repost", post.id)
                db.flush()
        except IntegrityError:
            # Concurrent duplicate toggle — converge to the existing row.
            from sqlalchemy.orm.attributes import instance_state
            if new_row is not None and instance_state(new_row).session_id is not None:
                db.expunge(new_row)
            row = db.scalar(select(model).where(model.user_id == me.id, model.post_id == post.id))
            if row is None:
                raise
        else:
            db.commit()
            return ToggleResponse(active=active)
    elif not active and row is not None:
        db.delete(row)
        db.commit()
    return ToggleResponse(active=active)


@feature_router.put("/posts/{post_id}/saved", response_model=ToggleResponse)
def save_post(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _toggle_post_feature(db, SavedPost, me, post_id, True)


@feature_router.delete("/posts/{post_id}/saved", response_model=ToggleResponse)
def unsave_post(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _toggle_post_feature(db, SavedPost, me, post_id, False)


@feature_router.put("/posts/{post_id}/hidden", response_model=ToggleResponse)
def hide_post(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    """Hide the post from the viewer's feeds (post stays readable directly)."""
    post = _visible_post(db, post_id, me.id)
    row = db.scalar(
        select(Mute).where(
            Mute.muted_by == me.id, Mute.post_id == post.id,
            Mute.target_id.is_(None),
        )
    )
    if row is None:
        hidden = None
        try:
            with db.begin_nested():
                hidden = Mute(muted_by=me.id, post_id=post.id)
                db.add(hidden)
                db.flush()
        except IntegrityError:
            # Concurrent duplicate hide — converge to the existing row.
            from sqlalchemy.orm.attributes import instance_state
            if hidden is not None and instance_state(hidden).session_id is not None:
                db.expunge(hidden)
            row = db.scalar(
                select(Mute).where(
                    Mute.muted_by == me.id, Mute.post_id == post.id,
                    Mute.target_id.is_(None),
                )
            )
            if row is None:
                raise
        else:
            db.commit()
            return ToggleResponse(active=True)
    return ToggleResponse(active=True)


@feature_router.delete("/posts/{post_id}/hidden", response_model=ToggleResponse)
def unhide_post(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    """Restore a hidden post to the viewer's feeds — idempotent."""
    _visible_post(db, post_id, me.id)
    row = db.scalar(
        select(Mute).where(
            Mute.muted_by == me.id, Mute.post_id == post_id,
            Mute.target_id.is_(None),
        )
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return ToggleResponse(active=False)


@feature_router.put("/posts/{post_id}/repost", response_model=ToggleResponse)
def repost(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _toggle_post_feature(db, Repost, me, post_id, True)


@feature_router.delete("/posts/{post_id}/repost", response_model=ToggleResponse)
def undo_repost(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    return _toggle_post_feature(db, Repost, me, post_id, False)


@feature_router.post(
    "/posts/{post_id}/media",
    response_model=PostMediaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_post_media(
    post_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Post not found."})
    if post.author_id != me.id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Only the author can add media."})
    from ting_ting.uploads import UploadRejected

    try:
        path, media_type = await store_post_upload(file, f"post-{me.id}", user_id=me.id)
    except UploadRejected as exc:
        status_code = 413 if exc.code in {"quota_exceeded", "storage_full"} else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    media = PostMedia(post_id=post.id, path=path, media_type=media_type)
    db.add(media)
    try:
        db.commit()
        db.refresh(media)
    except Exception:
        db.rollback()
        delete_stored_file(path)
        raise
    return PostMediaResponse(id=media.id, post_id=post.id, url=path, media_type=media_type)


@feature_router.delete("/posts/{post_id}/media/{media_id}")
def delete_post_media(
    post_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    post = db.get(Post, post_id)
    media = db.get(PostMedia, media_id)
    if post is None or media is None or media.post_id != post.id:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Media not found."})
    if post.author_id != me.id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Only the author can delete media."})
    path = media.path
    db.delete(media)
    db.commit()
    delete_stored_file(path)
    return {"message": "Media deleted."}
