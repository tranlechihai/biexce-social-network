"""Jinja2-rendered web routes for Ting Ting MVP.

Every page re-uses the existing API services through the same database session.
Authentication is handled via the same HttpOnly cookie (``ting_ting_auth``).
"""

from pathlib import Path
from urllib.parse import urlsplit, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting.auth import (
    clear_auth_cookie,
    create_token,
    get_current_user_web,
    hash_password,
    normalize_email,
    normalize_username,
    set_auth_cookie,
    verify_password,
)
from ting_ting.config import Settings, get_settings
from ting_ting.database import get_db
from ting_ting.models import (
    Activity, Follow, FriendRequest, Mute, PostMedia, Repost,
    SavedPost, User, UserProfile,
)
from ting_ting import notifications
from ting_ting import posts as posts_module
from ting_ting import sessions as session_service
from ting_ting.social import (
    cancel_sent_request,
    is_muted_by,
    relationship_state,
)
from ting_ting.posts import is_visible_to
from ting_ting.uploads import AVATAR_MAX as AVATAR_UPLOAD_MAX
from ting_ting.uploads import UploadRejected
from ting_ting.schemas import (
    POST_CONTENT_MAX,
    COMMENT_TEXT_MAX,
    DISPLAY_NAME_MAX,
    BIO_MAX,
    PASSWORD_MIN,
)
from ting_ting.interactions import (
    create_like, count_likes, count_comments, is_user_liked,
    create_comment, list_comments, delete_comment, edit_comment,
    remove_like,
)
from ting_ting import social as social_logic
from ting_ting.media import delete_stored_file
from ting_ting.security import csrf_token_for, require_csrf

# ---------------------------------------------------------------------------
# Router + templates
# ---------------------------------------------------------------------------

router = APIRouter(tags=["web"], dependencies=[Depends(require_csrf)])

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Disable Jinja2 template cache to avoid Python 3.14 hashability issues
_templates.env.cache = None


def _render(name: str, request: Request, **ctx) -> HTMLResponse:
    """Render a Jinja2 template.

    starlette's TemplateResponse signature:
        TemplateResponse(request, name, context, ...)
    """
    ctx.setdefault("active", "")
    ctx.setdefault("csrf_token", csrf_token_for(request))
    return _templates.TemplateResponse(request, name, {**ctx})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_summary(db: Session, user: User) -> dict:
    profile = db.get(UserProfile, user.id)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "bio": user.bio,
        "birthday": profile.birthday if profile else None,
        "gender": profile.gender if profile else None,
        "location": profile.location if profile else None,
        "occupation": profile.occupation if profile else None,
        "website": profile.website if profile else None,
        "avatar": ((profile.avatar_path or profile.avatar_url) if profile else None),
    }


async def _store_image(upload, prefix: str, max_bytes: int = 5 * 1024 * 1024) -> str | None:
    if not upload or not getattr(upload, "filename", ""):
        return None
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("image_too_large")
    signatures = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"RIFF", ".webp"))
    suffix = next((ext for magic, ext in signatures if data.startswith(magic)), None)
    if not suffix:
        raise ValueError("invalid_image")
    uploads = Path(__file__).resolve().parents[2] / "uploads"
    uploads.mkdir(exist_ok=True)
    filename = f"{prefix}-{uuid4().hex}{suffix}"
    (uploads / filename).write_bytes(data)
    return f"/media/{filename}"


async def _store_post_media(upload, user_id: int) -> tuple[str, str] | None:
    """Validate, scan and quota-check post media. Raises UploadRejected."""
    if not upload or not getattr(upload, "filename", ""):
        return None
    from ting_ting.uploads import (
        MAX_POST_MEDIA, check_upload_quota, validate_upload_bytes,
    )
    from ting_ting.media import UPLOADS_DIR
    from ting_ting.config import get_settings

    data = await upload.read(MAX_POST_MEDIA + 1)
    check_upload_quota(UPLOADS_DIR, user_id, len(data) if data else 0, get_settings())
    suffix, media_type = validate_upload_bytes(data, MAX_POST_MEDIA, allow_video=True)
    UPLOADS_DIR.mkdir(exist_ok=True)
    filename = f"post-{user_id}-{uuid4().hex}{suffix}"
    (UPLOADS_DIR / filename).write_bytes(data)
    return f"/media/{filename}", media_type


def _people_context(db: Session, me: User) -> dict:
    users = db.execute(sa_select(User).where(
        User.id != me.id, User.banned_at.is_(None),
    ).order_by(User.username)).scalars().all()
    people = []
    for user in users:
        state = relationship_state(db, me.id, user.id)
        if state != "blocked_by_them":
            followed = db.execute(sa_select(Follow).where(
                Follow.follower_id == me.id, Follow.followed_id == user.id,
            )).scalar_one_or_none() is not None
            people.append({
                **_user_summary(db, user),
                "relationship": state,
                "followed": followed,
                "muted": is_muted_by(db, me.id, user.id),
            })

    pending = social_logic.list_requests(db, me.id, "pending")
    incoming = []
    for item in pending:
        if item.recipient_id == me.id:
            sender = db.get(User, item.sender_id)
            if sender:
                incoming.append({"request_id": item.id, "user": _user_summary(db, sender)})

    friends = []
    for item in social_logic.list_friends(db, me.id):
        other = db.get(User, item.recipient_id if item.sender_id == me.id else item.sender_id)
        if other:
            friends.append(_user_summary(db, other))

    blocked = []
    for item in social_logic.list_blocks(db, me.id):
        other = db.get(User, item.blocked_id)
        if other:
            blocked.append(_user_summary(db, other))
    return {"people": people, "incoming": incoming, "friends": friends, "blocked": blocked}


def _comment_item(c, author_names: dict, viewer_id: int, post_author_id: int) -> dict:
    """Comment card dict — replies carry the parent's author for display."""
    return {
        "id": c.id,
        "author_name": author_names.get(c.id, "Unknown"),
        "content": c.content,
        "created_at_str": (
            c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else ""
        ),
        "is_reply": c.parent_comment_id is not None,
        "reply_to": author_names.get(c.parent_comment_id) if c.parent_comment_id else None,
        # Author or post-owner can delete; only the author can edit.
        "can_delete": c.author_id == viewer_id or post_author_id == viewer_id,
        "can_edit": c.author_id == viewer_id,
    }


def _post_item(db: Session, post, viewer_id: int) -> dict:
    from ting_ting.models import User as UserModel
    author = db.get(UserModel, post.author_id)
    author_profile = db.get(UserProfile, post.author_id)
    comments = list_comments(db, post.id)
    comment_items = []
    author_names: dict[int, str] = {}
    for c in comments:
        c_author = db.get(UserModel, c.author_id)
        author_names[c.id] = (
            (c_author.display_name or c_author.username) if c_author else "Unknown"
        )
    for c in comments:
        comment_items.append(_comment_item(c, author_names, viewer_id, post.author_id))
    return {
        "id": post.id,
        "author_name": author.display_name or author.username if author else "Unknown",
        "author_username": author.username if author else "unknown",
        "author_avatar": (author_profile.avatar_path or author_profile.avatar_url) if author_profile else None,
        "author_id": post.author_id,
        "content": post.content,
        "audience": post.audience,
        "created_at": post.created_at,
        "like_count": count_likes(db, post.id),
        "comment_count": count_comments(db, post.id),
        "liked_by_viewer": is_user_liked(db, viewer_id, post.id),
        "comments": comment_items,
        "media": [{"path": m.path, "type": m.media_type} for m in db.execute(sa_select(PostMedia).where(PostMedia.post_id == post.id)).scalars().all()],
        "repost_count": len(db.execute(sa_select(Repost).where(Repost.post_id == post.id)).scalars().all()),
        "reposted_by_viewer": db.execute(sa_select(Repost).where(Repost.post_id == post.id, Repost.user_id == viewer_id)).scalar_one_or_none() is not None,
        "saved_by_viewer": db.execute(sa_select(SavedPost).where(SavedPost.post_id == post.id, SavedPost.user_id == viewer_id)).scalar_one_or_none() is not None,
    }


def _feed_items(db: Session, feed_posts, viewer_id: int) -> list[dict]:
    """Build feed card dicts for many posts with grouped queries (no N+1)."""
    post_ids = [p.id for p in feed_posts]
    like_counts = posts_module.feed_like_counts(db, post_ids)
    comment_counts = posts_module.feed_comment_counts(db, post_ids)
    repost_counts = posts_module.feed_repost_counts(db, post_ids)
    liked, saved, reposted = posts_module.feed_viewer_states(db, viewer_id, post_ids)
    media_by_post = posts_module.feed_media(db, post_ids)
    comments_by_post = posts_module.feed_comments(db, post_ids)

    user_ids = {p.author_id for p in feed_posts}
    user_ids.update(c.author_id for comments in comments_by_post.values() for c in comments)
    users, profiles = posts_module.feed_authors(db, list(user_ids))

    items = []
    for p in feed_posts:
        author = users.get(p.author_id)
        profile = profiles.get(p.author_id)
        post_comments = comments_by_post.get(p.id, [])
        author_names: dict[int, str] = {}
        for c in post_comments:
            c_author = users.get(c.author_id)
            author_names[c.id] = (
                (c_author.display_name or c_author.username) if c_author else "Unknown"
            )
        comment_items = [
            _comment_item(c, author_names, viewer_id, p.author_id)
            for c in post_comments
        ]
        items.append({
            "id": p.id,
            "author_name": (author.display_name or author.username) if author else "Unknown",
            "author_username": author.username if author else "unknown",
            "author_avatar": (profile.avatar_path or profile.avatar_url) if profile else None,
            "author_id": p.author_id,
            "content": p.content,
            "audience": p.audience,
            "created_at": p.created_at,
            "like_count": like_counts.get(p.id, 0),
            "comment_count": comment_counts.get(p.id, 0),
            "liked_by_viewer": p.id in liked,
            "comments": comment_items,
            "media": [{"path": m.path, "type": m.media_type} for m in media_by_post.get(p.id, [])],
            "repost_count": repost_counts.get(p.id, 0),
            "reposted_by_viewer": p.id in reposted,
            "saved_by_viewer": p.id in saved,
        })
    return items


def _profile_posts(db: Session, user_id: int, viewer_id: int) -> list[dict]:
    from ting_ting.models import Post
    rows = db.execute(sa_select(Post).where(Post.author_id == user_id).order_by(Post.created_at.desc())).scalars().all()
    result = []
    for post in rows:
        if is_visible_to(post.author_id, viewer_id, post.audience, db):
            item = _post_item(db, post, viewer_id)
            item["is_own"] = post.author_id == viewer_id
            item["created_at_str"] = post.created_at.strftime("%d/%m/%Y") if post.created_at else ""
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Layout shortcut
# ---------------------------------------------------------------------------

@router.get("/")
async def index(request: Request):
    return RedirectResponse(url="/web/feed")


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@router.get("/register")
async def register_page(request: Request):
    return _render("register.html", request, active="register")


@router.post("/register")
async def register_submit(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    form = await request.form()
    username = form.get("username", "").strip()
    email = form.get("email", "").strip()
    password = form.get("password", "")

    # Validate
    errors = []
    if len(username) < 3 or len(username) > 30:
        errors.append("Tên người dùng phải dài 3-30 ký tự.")
    import re
    if username and not re.match(r"^[a-z0-9_]+$", username.strip().lower()):
        errors.append("Tên người dùng chỉ được dùng chữ thường, số và dấu gạch dưới.")
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("Vui lòng nhập email hợp lệ.")
    if len(password) < 8:
        errors.append("Mật khẩu phải dài ít nhất 8 ký tự.")

    if errors:
        return _render("register.html", request, errors=errors, active="register",
                        old={"username": username, "email": email})

    user = db.execute(
        sa_select(User).where(
            (User.username == normalize_username(username)) |
            (User.email == normalize_email(email))
        )
    ).scalar_one_or_none()

    if user:
        return _render("register.html", request,
                        errors=["Tên người dùng hoặc email này đã tồn tại."],
                        active="register",
                        old={"username": username, "email": email})

    new_user = User(
        username=normalize_username(username),
        email=normalize_email(email),
        password_hash=hash_password(password),
    )
    db.add(new_user)
    db.flush()  # assign new_user.id before the session row references it
    session = session_service.create_session(db, new_user.id, settings)
    db.commit()
    db.refresh(new_user)

    # Auto-login
    token = create_token(new_user.id, new_user.username, session.id, settings)
    redirect = RedirectResponse(url="/web/feed", status_code=303)
    set_auth_cookie(redirect, token, secure=settings.cookie_secure)
    return redirect


@router.get("/login")
async def login_page(request: Request):
    return _render("login.html", request, active="login")


@router.post("/login")
async def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    form = await request.form()
    identifier = form.get("identifier", "").strip()
    password = form.get("password", "")

    user = db.execute(
        sa_select(User).where(
            (User.username == normalize_username(identifier)) |
            (User.email == normalize_email(identifier))
        )
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        return _render("login.html", request,
                        errors=["Sai tên đăng nhập hoặc mật khẩu."],
                        active="login",
                        old={"identifier": identifier})

    if user.banned_at is not None:
        return _render("login.html", request,
                        errors=["Your account has been temporarily locked by a moderator."],
                        active="login",
                        old={"identifier": identifier})

    session = session_service.create_session(db, user.id, settings)
    db.commit()
    token = create_token(user.id, user.username, session.id, settings)
    redirect = RedirectResponse(url="/web/feed", status_code=303)
    set_auth_cookie(redirect, token, secure=settings.cookie_secure)
    return redirect


@router.post("/logout")
async def logout_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_web),
):
    # Revoke the server-side session so the JWT dies even if the cookie leaks.
    session_id = getattr(request.state, "sid", None)
    if session_id:
        session_service.revoke_session(db, session_id)
        db.commit()
    redirect = RedirectResponse(url="/web/login", status_code=303)
    clear_auth_cookie(redirect)
    return redirect


@router.post("/profile/logout-all")
async def logout_all_submit(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_web),
):
    """Revoke every session for this account, then log out here."""
    session_service.revoke_all_sessions(db, user.id)
    db.commit()
    redirect = RedirectResponse(url="/web/login", status_code=303)
    clear_auth_cookie(redirect)
    return redirect


# ---------------------------------------------------------------------------
# Feed / Posts
# ---------------------------------------------------------------------------

@router.get("/feed")
async def feed_page(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.posts import query_feed as query_feed_service

    view = request.query_params.get("view", "for-you")
    if view not in {"for-you", "following"}:
        view = "for-you"
    if view == "following":
        feed_posts, _ = posts_module.query_following_feed(db, me.id, limit=50)
    else:
        feed_posts, _ = query_feed_service(db, me.id, limit=50)
    posts = []
    for p in _feed_items(db, feed_posts, me.id):
        p["created_at_str"] = p["created_at"].strftime("%Y-%m-%d %H:%M") if p["created_at"] else ""
        p["is_own"] = p["author_id"] == me.id
        posts.append(p)

    error_code = request.query_params.get("error", "")
    errors = None
    if error_code in UPLOAD_ERROR_MESSAGES:
        errors = [UPLOAD_ERROR_MESSAGES[error_code]]
    return _render("feed.html", request,
                    posts=posts, username=me.username, active="feed", feed_view=view,
                    suggestions=_people_context(db, me)["people"][:5],
                    errors=errors)


@router.get("/thread/new")
async def new_thread_page(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    return _render(
        "thread_new.html", request, username=me.username, active="create",
        user=_user_summary(db, me), suggestions=_people_context(db, me)["people"][:5],
    )


@router.post("/posts/create")
async def create_post_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.posts import create_post as create_post_service

    form = await request.form()
    content = form.get("content", "").strip()
    audience = form.get("audience", "ONLY_ME").strip()

    if not content:
        return RedirectResponse(url="/web/feed?error=empty", status_code=303)
    if len(content) > POST_CONTENT_MAX:
        return RedirectResponse(url="/web/feed?error=too_long", status_code=303)
    if audience not in ("ONLY_ME", "FRIENDS", "PUBLIC"):
        return RedirectResponse(
            url="/web/feed?error=invalid_audience", status_code=303,
        )

    post = create_post_service(db, me.id, content, audience)
    from ting_ting.uploads import UploadRejected

    try:
        media = await _store_post_media(form.get("media_file"), me.id)
    except UploadRejected as exc:
        db.rollback()
        if request.headers.get("x-file-upload") == "xhr":
            # XHR cannot observe 303 redirect targets — return the rejection
            # as a JSON error so the composer can show the reason inline.
            return JSONResponse(
                status_code=422,
                content={"error": {"code": exc.code, "message": UPLOAD_ERROR_MESSAGES.get(exc.code, exc.code)}},
            )
        return RedirectResponse(url=f"/web/feed?error={exc.code}", status_code=303)
    media_path = None
    if media:
        media_path, media_type = media
        db.add(PostMedia(post_id=post.id, path=media_path, media_type=media_type))
    try:
        db.commit()
    except Exception:
        db.rollback()
        if media_path:
            delete_stored_file(media_path)
        raise
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/edit")
async def edit_post_submit(
    request: Request,
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.posts import edit_post as edit_post_service
    from ting_ting.models import Post

    form = await request.form()
    content = form.get("content", "").strip()
    # Distinguish: field absent (not in form) → skip; blank or invalid → reject
    if "audience" in form:
        audience = form["audience"].strip()
    else:
        audience = None

    # Enforce same validation as API PostUpdateRequest schema:
    # content min_length=1 (reject empty), max_length=POST_CONTENT_MAX
    # audience, if submitted, must be ONLY_ME, FRIENDS or PUBLIC
    if not content:
        return RedirectResponse(url="/web/feed?error=empty", status_code=303)
    if len(content) > POST_CONTENT_MAX:
        return RedirectResponse(url="/web/feed?error=too_long", status_code=303)
    if audience is not None:
        if audience not in ("ONLY_ME", "FRIENDS", "PUBLIC"):
            return RedirectResponse(
                url="/web/feed?error=invalid_audience", status_code=303,
            )

    post = db.get(Post, post_id)
    if not post:
        return RedirectResponse(url="/web/feed", status_code=303)
    if post.author_id != me.id:
        return RedirectResponse(url="/web/feed", status_code=303)

    edit_post_service(db, post, me.id, content=content, audience=audience)
    db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/delete")
async def delete_post_submit(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post
    from ting_ting.posts import delete_post as delete_post_service

    post = db.get(Post, post_id)
    if not post or post.author_id != me.id:
        return RedirectResponse(url="/web/feed", status_code=303)
    media_paths = [
        row.path for row in db.execute(
            sa_select(PostMedia).where(PostMedia.post_id == post.id)
        ).scalars().all()
    ]
    db.execute(PostMedia.__table__.delete().where(PostMedia.post_id == post.id))
    delete_post_service(db, post, me.id)
    db.commit()
    for media_path in media_paths:
        delete_stored_file(media_path)
    return RedirectResponse(url="/web/feed", status_code=303)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _follow_counts(db: Session, user_id: int) -> tuple[int, int]:
    """(following, followers) counts for a user."""
    from sqlalchemy import func

    following = db.execute(
        sa_select(func.count()).select_from(Follow).where(Follow.follower_id == user_id)
    ).scalar_one()
    followers = db.execute(
        sa_select(func.count()).select_from(Follow).where(Follow.followed_id == user_id)
    ).scalar_one()
    return int(following), int(followers)


@router.get("/profile/me")
async def my_profile_page(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    following, followers = _follow_counts(db, me.id)
    return _render("profile.html", request,
                    user=_user_summary(db, me), username=me.username,
                    is_own=True, active="profile", posts=_profile_posts(db, me.id, me.id),
                    following_count=following, followers_count=followers,
                    password_changed=request.query_params.get("password_changed") == "1")


@router.post("/profile/password")
async def change_password_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    errors = []
    current_too_long = len(current_password.encode("utf-8")) > 72
    if not current_password or current_too_long or not verify_password(current_password, me.password_hash):
        errors.append("Mật khẩu hiện tại không đúng.")
    elif new_password != confirm_password:
        errors.append("Mật khẩu xác nhận không khớp.")
    elif len(new_password) < PASSWORD_MIN:
        errors.append(f"Mật khẩu mới phải có ít nhất {PASSWORD_MIN} ký tự.")
    elif len(new_password.encode("utf-8")) > 72:
        errors.append("Mật khẩu mới không được vượt quá 72 byte.")
    elif verify_password(new_password, me.password_hash):
        errors.append("Mật khẩu mới phải khác mật khẩu hiện tại.")

    if errors:
        return _render(
            "profile.html", request, errors=errors,
            user=_user_summary(db, me), username=me.username,
            is_own=True, active="profile", posts=_profile_posts(db, me.id, me.id),
            password_panel_open=True,
        )

    me.password_hash = hash_password(new_password)
    # Rotate credentials: kill every other session, keep the current one.
    current_sid = getattr(request.state, "sid", None)
    session_service.revoke_all_sessions(db, me.id, keep_session_id=current_sid)
    db.commit()
    return RedirectResponse(url="/web/profile/me?password_changed=1", status_code=303)


@router.get("/profile/{username}")
async def user_profile_page(
    request: Request,
    username: str,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    target = db.execute(
        sa_select(User).where(
            User.username == username, User.banned_at.is_(None),
        )
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    rel = relationship_state(db, me.id, target.id)
    following = db.execute(
        sa_select(Follow).where(Follow.follower_id == me.id, Follow.followed_id == target.id)
    ).scalar_one_or_none() is not None
    muted = is_muted_by(db, me.id, target.id)
    outgoing_request_id = None
    if rel == "pending_outgoing":
        left, right = social_logic.canonical_pair(me.id, target.id)
        req = db.execute(
            sa_select(FriendRequest).where(
                FriendRequest.canonical_left == left,
                FriendRequest.canonical_right == right,
                FriendRequest.state == "pending",
            )
        ).scalar_one_or_none()
        if req is not None and req.sender_id == me.id:
            outgoing_request_id = req.id

    followers_count = None
    following_count = None
    if rel in ("blocked_by_me", "blocked_by_them"):
        # Block privacy: a blocked pair sees only the minimum identity needed
        # to manage the block — no bio, contacts, avatar, or posts.
        summary = _user_summary(db, target)
        for field in (
            "display_name", "bio", "birthday", "gender", "location",
            "occupation", "website", "avatar",
        ):
            summary[field] = None
        posts = []
    else:
        summary = _user_summary(db, target)
        posts = _profile_posts(db, target.id, me.id)
        following_count, followers_count = _follow_counts(db, target.id)

    return _render("profile.html", request,
                    user=summary, username=me.username,
                    is_own=target.id == me.id, relationship=rel, following=following,
                    muted=muted, outgoing_request_id=outgoing_request_id,
                    active="profile", posts=posts,
                    following_count=following_count, followers_count=followers_count)


@router.get("/people")
async def people_page(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    query = request.query_params.get("q", "").strip().lower()[:60]
    context = _people_context(db, me)
    if query:
        context["people"] = [
            person for person in context["people"]
            if query in person["username"].lower()
            or query in (person.get("display_name") or "").lower()
        ]
    return _render(
        "people.html", request, username=me.username, active="people",
        query=query, **context,
    )


@router.post("/profile/update")
async def update_profile_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    # Strip but keep empty string — API ProfileUpdateRequest.display_name has
    # min_length=0, so empty string is valid and intentionally clears the field.
    display_name = form.get("display_name", "").strip()
    bio = form.get("bio", "").strip()
    birthday = form.get("birthday", "").strip()
    gender = form.get("gender", "").strip()
    location = form.get("location", "").strip()
    occupation = form.get("occupation", "").strip()
    website = form.get("website", "").strip()
    avatar_url = form.get("avatar_url", "").strip()

    # Enforce same validation as API ProfileUpdateRequest schema:
    # reject oversize (API returns 422), persist no altered data
    if len(display_name) > DISPLAY_NAME_MAX:
        return _render(
            "profile.html", request,
            errors=[f"display_name must be at most {DISPLAY_NAME_MAX} characters."],
            user=_user_summary(db, me),
            username=me.username,
            is_own=True,
            active="profile",
        )
    if len(bio) > BIO_MAX:
        return _render(
            "profile.html", request,
            errors=[f"bio must be at most {BIO_MAX} characters."],
            user=_user_summary(db, me),
            username=me.username,
            is_own=True,
            active="profile",
        )

    limits = {"location": (location, 100), "occupation": (occupation, 100), "website": (website, 300)}
    if any(len(value) > limit for value, limit in limits.values()):
        return _render("profile.html", request, errors=["Một hoặc nhiều trường hồ sơ vượt quá độ dài cho phép."],
                       user=_user_summary(db, me), username=me.username, is_own=True, active="profile")
    if gender not in ("", "female", "male", "non_binary", "prefer_not_to_say"):
        return _render("profile.html", request, errors=["Giá trị giới tính không hợp lệ."],
                       user=_user_summary(db, me), username=me.username, is_own=True, active="profile")
    for candidate in (website, avatar_url):
        if candidate and urlparse(candidate).scheme not in ("http", "https"):
            return _render("profile.html", request, errors=["Website and avatar URL must use http or https."],
                           user=_user_summary(db, me), username=me.username, is_own=True, active="profile")

    profile = db.get(UserProfile, me.id) or UserProfile(user_id=me.id)
    db.add(profile)
    profile.birthday = birthday or None
    profile.gender = gender or None
    profile.location = location or None
    profile.occupation = occupation or None
    profile.website = website or None
    profile.avatar_url = avatar_url or None

    upload = form.get("avatar_file")
    if upload and getattr(upload, "filename", ""):
        _new_avatar_path, avatar_error = await _validate_and_store_avatar(db, me, upload)
        if avatar_error:
            return _render("profile.html", request, errors=[UPLOAD_ERROR_MESSAGES_AVATAR.get(avatar_error, avatar_error)],
                           user=_user_summary(db, me), username=me.username, is_own=True, active="profile")

    me.display_name = display_name
    me.bio = bio
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(url="/web/profile/me", status_code=303)


async def _validate_and_store_avatar(db: Session, me: User, upload) -> tuple[str | None, str | None]:
    """Shared avatar ingest: validate, scan, quota-check, store, commit.

    Returns ``(new_path, old_path)`` on success or ``(None, error_code)``
    when rejected — nothing is stored in that case.
    """
    from ting_ting.config import get_settings
    from ting_ting.media import UPLOADS_DIR
    from ting_ting.uploads import check_upload_quota, validate_upload_bytes

    data = await upload.read(AVATAR_UPLOAD_MAX + 1)
    try:
        check_upload_quota(UPLOADS_DIR, me.id, len(data) if data else 0, get_settings())
        suffix, _media_type = validate_upload_bytes(data, AVATAR_UPLOAD_MAX, allow_video=False)
    except UploadRejected as exc:
        return None, exc.code
    profile = db.get(UserProfile, me.id) or UserProfile(user_id=me.id)
    db.add(profile)
    old_path = profile.avatar_path
    UPLOADS_DIR.mkdir(exist_ok=True)
    filename = f"avatar-{me.id}-{uuid4().hex}{suffix}"
    (UPLOADS_DIR / filename).write_bytes(data)
    profile.avatar_path = f"/media/{filename}"
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_stored_file(profile.avatar_path)
        raise
    if old_path and old_path != profile.avatar_path:
        delete_stored_file(old_path)
    return profile.avatar_path, None


UPLOAD_ERROR_MESSAGES = {
    "media_too_large": "File media vượt quá 25 MB.",
    "invalid_media": "File phải là JPEG, PNG, WebP, MP4 hoặc WebM.",
    "blocked_content": "File chứa nội dung bị cấm (executable/archive) và đã bị chặn.",
    "quota_exceeded": "Đã đạt hạn mức lưu trữ cá nhân. Xóa bài viết/media cũ để giải phóng dung lượng.",
    "storage_full": "Máy chủ đã đạt hạn mức lưu trữ. Vui lòng thử lại sau.",
    "upload_failed": "Đăng bài thất bại. Vui lòng thử lại.",
}
UPLOAD_ERROR_MESSAGES_AVATAR = {
    "media_too_large": "Ảnh đại diện phải nhỏ hơn 2 MB.",
    "invalid_media": "Ảnh đại diện phải là JPEG, PNG hoặc WebP.",
    "blocked_content": "File chứa nội dung bị cấm (executable/archive) và đã bị chặn.",
    "quota_exceeded": "Đã đạt hạn mức lưu trữ cá nhân. Xóa media cũ để giải phóng dung lượng.",
    "storage_full": "Máy chủ đã đạt hạn mức lưu trữ. Vui lòng thử lại sau.",
}


@router.post("/avatar/upload")
async def upload_avatar_quick(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    upload = form.get("avatar_file")
    errors = []
    if not upload or not getattr(upload, "filename", ""):
        errors = ["Hãy chọn một tệp ảnh trước."]
        return _render("profile.html", request, errors=errors,
                       user=_user_summary(db, me), username=me.username, is_own=True, active="profile")
    new_path, error_code = await _validate_and_store_avatar(db, me, upload)
    if error_code:
        errors = [UPLOAD_ERROR_MESSAGES_AVATAR.get(error_code, error_code)]
        return _render("profile.html", request, errors=errors,
                       user=_user_summary(db, me), username=me.username, is_own=True, active="profile")
    return RedirectResponse(url="/web/profile/me", status_code=303)


# ---------------------------------------------------------------------------
# Social actions (friend requests, block, etc.)
# ---------------------------------------------------------------------------

@router.post("/social/friend-request")
async def send_friend_request_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    target_username = form.get("target_username", "").strip()

    target = db.execute(
        sa_select(User).where(User.username == target_username)
    ).scalar_one_or_none()
    if not target:
        return _render("profile.html", request, errors=["User not found."],
                        user=_user_summary(db, me), username=me.username,
                        is_own=True, active="profile")

    try:
        social_logic.create_friend_request(db, me, target)
        db.commit()
    except ValueError:
        db.rollback()
        return _render("profile.html", request, errors=["Không thể gửi lời mời kết bạn."],
                        user=_user_summary(db, me), username=me.username,
                        is_own=True, active="profile")

    return RedirectResponse(url=f"/web/profile/{target.username}", status_code=303)


@router.post("/social/accept-request")
async def accept_request_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    request_id = int(form.get("request_id", 0))
    from ting_ting.models import FriendRequest
    req = db.get(FriendRequest, request_id)
    if not req:
        return RedirectResponse(url="/web/profile/me", status_code=303)
    try:
        social_logic.accept_friend_request(db, req, me)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/profile/me", status_code=303)


@router.post("/social/reject-request")
async def reject_request_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    request_id = int(form.get("request_id", 0))
    from ting_ting.models import FriendRequest
    req = db.get(FriendRequest, request_id)
    if not req:
        return RedirectResponse(url="/web/profile/me", status_code=303)
    try:
        social_logic.reject_friend_request(db, req, me)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/profile/me", status_code=303)


@router.post("/social/unfriend")
async def unfriend_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    target_username = form.get("target_username", "").strip()
    target = db.execute(
        sa_select(User).where(User.username == target_username)
    ).scalar_one_or_none()
    if not target:
        return RedirectResponse(url="/web/feed", status_code=303)
    try:
        social_logic.unfriend(db, me.id, target.id, me)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url=f"/web/profile/{target.username}", status_code=303)


@router.post("/social/block")
async def block_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    target_username = form.get("target_username", "").strip()
    target = db.execute(
        sa_select(User).where(User.username == target_username)
    ).scalar_one_or_none()
    if not target:
        return RedirectResponse(url="/web/feed", status_code=303)
    try:
        social_logic.block_user(db, me, target)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/social/unblock")
async def unblock_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    form = await request.form()
    target_username = form.get("target_username", "").strip()
    target = db.execute(
        sa_select(User).where(User.username == target_username)
    ).scalar_one_or_none()
    if not target:
        return RedirectResponse(url="/web/feed", status_code=303)
    removed = social_logic.unblock_user(db, me, target.id)
    if removed:
        db.commit()
    return RedirectResponse(url=f"/web/profile/{target.username}", status_code=303)


@router.post("/social/cancel-request")
async def cancel_request_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    """Cancel a sent, still-pending friend request (sender only)."""
    form = await request.form()
    request_id = int(form.get("request_id", 0))
    from ting_ting.models import FriendRequest
    req = db.get(FriendRequest, request_id)
    if not req:
        return RedirectResponse(url="/web/feed", status_code=303)
    next_url = f"/web/profile/{_username_of(db, req.recipient_id)}"
    try:
        cancel_sent_request(db, req, me)
        db.commit()
    except ValueError:
        db.rollback()
        next_url = "/web/feed"
    return RedirectResponse(url=next_url, status_code=303)


def _username_of(db: Session, user_id: int) -> str:
    user = db.get(User, user_id)
    return user.username if user else "unknown"


@router.post("/social/mute")
async def mute_toggle_submit(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    """Toggle a mute on a user (unilateral, no relationship effect)."""
    form = await request.form()
    next_url = _safe_next(request, form.get("next", ""))
    target = db.execute(
        sa_select(User).where(User.username == form.get("target_username", ""))
    ).scalar_one_or_none()
    if target and target.id != me.id:
        if social_logic.is_muted_by(db, me.id, target.id):
            social_logic.unmute_user(db, me, target.id)
        else:
            social_logic.mute_user(db, me, target.id)
        db.commit()
    return RedirectResponse(url=next_url, status_code=303)


# ---------------------------------------------------------------------------
# Like / Comment actions
# ---------------------------------------------------------------------------

@router.post("/social/follow")
async def follow_submit(request: Request, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    form = await request.form()
    next_url = _safe_next(request, form.get("next", ""))
    target = db.execute(sa_select(User).where(User.username == form.get("target_username", ""))).scalar_one_or_none()
    if target and target.id != me.id and not social_logic.is_blocked(db, me.id, target.id):
        existing = db.execute(sa_select(Follow).where(Follow.follower_id == me.id, Follow.followed_id == target.id)).scalar_one_or_none()
        if not existing:
            try:
                with db.begin_nested():
                    db.add(Follow(follower_id=me.id, followed_id=target.id))
                    notifications.record(db, target.id, me.id, "follow")
                    db.flush()
            except IntegrityError:
                pass
            else:
                db.commit()
    return RedirectResponse(url=next_url, status_code=303)


def _safe_next(request: Request, raw: str) -> str:
    candidates = [raw or ""]
    referer = request.headers.get("referer", "")
    if referer:
        try:
            candidates.append(urlsplit(referer).path)
        except ValueError:
            pass
    for candidate in candidates:
        if candidate.startswith("/web/") and not candidate.startswith("//"):
            return candidate
    return "/web/people"


@router.post("/social/unfollow")
async def unfollow_submit(request: Request, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    form = await request.form()
    next_url = _safe_next(request, form.get("next", ""))
    target = db.execute(sa_select(User).where(User.username == form.get("target_username", ""))).scalar_one_or_none()
    if target:
        row = db.execute(sa_select(Follow).where(Follow.follower_id == me.id, Follow.followed_id == target.id)).scalar_one_or_none()
        if row:
            if social_logic.is_blocked(db, me.id, target.id):
                return RedirectResponse(url=next_url, status_code=303)
            db.delete(row)
            db.commit()
    return RedirectResponse(url=next_url, status_code=303)


@router.get("/activity")
async def activity_page(request: Request, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    activity_kind = request.query_params.get("kind", "")
    if activity_kind not in {"like", "comment", "follow"}:
        activity_kind = ""
    rows, _ = notifications.list_notifications(
        db, me.id, limit=100, kind=activity_kind or None,
    )
    items = []
    for row in rows:
        actor = db.get(User, row.actor_id)
        if actor:
            actor_followed = db.execute(sa_select(Follow).where(
                Follow.follower_id == me.id, Follow.followed_id == actor.id,
            )).scalar_one_or_none() is not None
            items.append({
                "id": row.id,
                "actor": _user_summary(db, actor),
                "kind": row.kind,
                "post_id": row.post_id,
                "created_at": row.created_at,
                "actor_followed": actor_followed,
                "is_read": row.read_at is not None,
            })
    return _render(
        "activity.html", request, username=me.username, active="activity",
        activities=items, activity_kind=activity_kind,
        unread_count=notifications.unread_count(db, me.id),
        suggestions=_people_context(db, me)["people"][:5],
    )


@router.post("/activity/read-all")
async def activity_read_all(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    notifications.mark_all_read(db, me.id)
    db.commit()
    return RedirectResponse(url="/web/activity", status_code=303)


@router.post("/activity/{activity_id}/read")
async def activity_mark_read(
    activity_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    row = db.get(Activity, activity_id)
    notifications.mark_read(db, me.id, row)
    db.commit()
    return RedirectResponse(url="/web/activity", status_code=303)


@router.get("/saved")
async def saved_page(request: Request, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    from ting_ting.models import Post
    rows = db.execute(sa_select(SavedPost).where(SavedPost.user_id == me.id).order_by(SavedPost.created_at.desc())).scalars().all()
    posts = []
    for row in rows:
        post = db.get(Post, row.post_id)
        if post and is_visible_to(post.author_id, me.id, post.audience, db):
            item = _post_item(db, post, me.id)
            item["is_own"] = post.author_id == me.id
            item["created_at_str"] = post.created_at.strftime("%Y-%m-%d %H:%M") if post.created_at else ""
            posts.append(item)
    return _render("saved.html", request, username=me.username, active="saved", posts=posts)


@router.post("/posts/{post_id}/save")
async def save_post_submit(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    from ting_ting.models import Post
    post = db.get(Post, post_id)
    if post and is_visible_to(post.author_id, me.id, post.audience, db):
        row = db.execute(sa_select(SavedPost).where(SavedPost.user_id == me.id, SavedPost.post_id == post_id)).scalar_one_or_none()
        if row:
            db.delete(row)
        else:
            db.add(SavedPost(user_id=me.id, post_id=post_id))
        db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/repost")
async def repost_submit(post_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user_web)):
    from ting_ting.models import Post
    post = db.get(Post, post_id)
    if post and is_visible_to(post.author_id, me.id, post.audience, db):
        row = db.execute(sa_select(Repost).where(Repost.user_id == me.id, Repost.post_id == post_id)).scalar_one_or_none()
        if row:
            db.delete(row)
        else:
            db.add(Repost(user_id=me.id, post_id=post_id))
            notifications.record(db, post.author_id, me.id, "repost", post_id)
        db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)

@router.post("/posts/{post_id}/like")
async def like_submit(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post
    from ting_ting.posts import is_visible_to

    post = db.get(Post, post_id)
    if not post:
        return RedirectResponse(url="/web/feed", status_code=303)
    if not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)

    create_like(db, me.id, post)
    notifications.record(db, post.author_id, me.id, "like", post.id)
    db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/unlike")
async def unlike_submit(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post
    from ting_ting.posts import is_visible_to

    post = db.get(Post, post_id)
    if not post:
        return RedirectResponse(url="/web/feed", status_code=303)
    if not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)

    remove_like(db, me.id, post)
    db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/hide")
async def hide_post_submit(
    post_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    """Hide a visible post from the viewer's feeds (toggle)."""
    from ting_ting.models import Post

    post = db.get(Post, post_id)
    if not post or not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)
    row = db.execute(
        sa_select(Mute).where(
            Mute.muted_by == me.id, Mute.post_id == post_id,
            Mute.target_id.is_(None),
        )
    ).scalar_one_or_none()
    if row is not None:
        db.delete(row)
    else:
        db.add(Mute(muted_by=me.id, post_id=post_id))
    db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/comment")
async def comment_submit(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post
    from ting_ting.posts import is_visible_to

    post = db.get(Post, post_id)
    if not post:
        return RedirectResponse(url="/web/feed", status_code=303)
    if not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)

    form = await request.form()
    content = form.get("content", "").strip()
    if not content:
        return RedirectResponse(url="/web/feed?error=empty_comment", status_code=303)
    if len(content) > COMMENT_TEXT_MAX:
        return RedirectResponse(url="/web/feed?error=comment_too_long", status_code=303)

    parent_comment_id = None
    raw_parent = form.get("parent_comment_id", "").strip()
    if raw_parent:
        try:
            parent_comment_id = int(raw_parent)
        except ValueError:
            parent_comment_id = None

    from ting_ting.models import Comment
    try:
        comment = create_comment(db, post, me.id, content, parent_comment_id=parent_comment_id)
    except ValueError:
        db.rollback()
        return RedirectResponse(url="/web/feed", status_code=303)

    recipient_ids = {post.author_id}
    if comment.parent_comment_id is not None:
        parent = db.get(Comment, comment.parent_comment_id)
        if parent is not None:
            recipient_ids.add(parent.author_id)
    for recipient_id in recipient_ids:
        notifications.record(db, recipient_id, me.id, "comment", post.id)
    db.commit()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/comments/{comment_id}/edit")
async def edit_comment_submit(
    post_id: int,
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post, Comment
    from ting_ting.posts import is_visible_to

    post = db.get(Post, post_id)
    comment = db.get(Comment, comment_id)
    if not post or not comment or comment.post_id != post_id:
        return RedirectResponse(url="/web/feed", status_code=303)
    if not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)

    form = await request.form()
    content = form.get("content", "").strip()
    if not content or len(content) > COMMENT_TEXT_MAX:
        return RedirectResponse(url="/web/feed", status_code=303)

    try:
        edit_comment(db, comment, me.id, content)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/feed", status_code=303)


@router.post("/posts/{post_id}/comments/{comment_id}/delete")
async def delete_comment_submit(
    post_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting.models import Post, Comment
    from ting_ting.posts import is_visible_to

    post = db.get(Post, post_id)
    comment = db.get(Comment, comment_id)
    if not post or not comment or comment.post_id != post_id:
        return RedirectResponse(url="/web/feed", status_code=303)
    if not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)

    try:
        delete_comment(db, comment, me.id, post)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/feed", status_code=303)


# ---------------------------------------------------------------------------
# Reports & moderation (web)
# ---------------------------------------------------------------------------

REPORT_REASON_LABELS = {
    "spam": "Spam / quảng cáo",
    "harassment": "Quấy rối / khiêu khích",
    "hate_speech": "Ngôn từ thù ghét",
    "false_info": "Thông tin sai sự thật",
    "other": "Khác",
}


@router.post("/posts/{post_id}/report")
async def report_post_submit(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    """Report a post or a comment on it (visibility enforced at submit)."""
    from ting_ting.models import Comment, Post
    from ting_ting.posts import is_visible_to
    from ting_ting import moderation as mod

    post = db.get(Post, post_id)
    comment = None
    form = await request.form()
    raw_cid = (form.get("comment_id") or "").strip()
    if raw_cid:
        try:
            comment = db.get(Comment, int(raw_cid))
        except ValueError:
            comment = None
    if not post or not is_visible_to(post.author_id, me.id, post.audience, db):
        return RedirectResponse(url="/web/feed", status_code=303)
    if comment is not None and comment.post_id != post_id:
        return RedirectResponse(url="/web/feed", status_code=303)

    reason = form.get("reason", "other")
    try:
        # Comment reports anchor to the comment's own post so the content is
        # addressable in the queue.
        mod.create_report(
            db,
            me,
            post.author_id,
            reason=reason,
            post_id=post_id,
            comment_id=comment.id if comment else None,
        )
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(url="/web/feed?reported=1", status_code=303)


def _require_moderator(me: User):
    if not me.is_moderator:
        raise HTTPException(status_code=403, detail="Chức năng dành riêng cho điều phối viên.")


@router.get("/mod/reports")
async def mod_reports_page(
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
    status: str = None,
):
    from ting_ting import moderation as mod
    from ting_ting.models import User as UserModel

    _require_moderator(me)
    status = status if status in {"pending", "resolved", "dismissed"} else "pending"
    reports = mod.list_reports(db, status_filter=status, limit=100)

    rows = []
    for r in reports:
        reporter = db.get(UserModel, r.reporter_id)
        target = db.get(UserModel, r.target_user_id)
        rows.append({
            "id": r.id,
            "reason_label": REPORT_REASON_LABELS.get(r.reason, r.reason),
            "status": r.status,
            "resolution_note": r.resolution_note,
            "created_at_str": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            "resolved_at_str": r.resolved_at.strftime("%Y-%m-%d %H:%M") if r.resolved_at else "",
            "reporter": reporter.username if reporter else "unknown",
            "target": target.username if target else "unknown",
            "target_id": r.target_user_id,
            "post_id": r.post_id,
            "comment_id": r.comment_id,
        })
    return _render("mod_reports.html", request,
                    reports=rows, active="reports",
                    current_status=status, username=me.username)


@router.post("/mod/reports/{report_id}/resolve")
async def mod_report_resolve(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting import moderation as mod
    from ting_ting.models import Report

    _require_moderator(me)
    report = db.get(Report, report_id)
    if report:
        try:
            mod.resolve_report(
                db, me, report,
                note=(await request.form()).get("note", "").strip() or None,
            )
            db.commit()
        except ValueError:
            db.rollback()
    return RedirectResponse(url="/web/mod/reports", status_code=303)


@router.post("/mod/reports/{report_id}/dismiss")
async def mod_report_dismiss(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting import moderation as mod
    from ting_ting.models import Report

    _require_moderator(me)
    report = db.get(Report, report_id)
    if report:
        try:
            mod.resolve_report(
                db, me, report, dismiss=True,
                note=(await request.form()).get("note", "").strip() or None,
            )
            db.commit()
        except ValueError:
            db.rollback()
    return RedirectResponse(url="/web/mod/reports", status_code=303)


@router.post("/mod/users/{user_id}/ban")
async def mod_ban_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting import moderation as mod
    from ting_ting.models import User as UserModel

    _require_moderator(me)
    target = db.get(UserModel, user_id)
    if target:
        try:
            mod.ban_user(db, me, target)
            db.commit()
        except ValueError:
            db.rollback()
    return RedirectResponse(url="/web/mod/reports", status_code=303)


@router.post("/mod/users/{user_id}/unban")
async def mod_unban_user(
    user_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user_web),
):
    from ting_ting import moderation as mod
    from ting_ting.models import User as UserModel

    _require_moderator(me)
    target = db.get(UserModel, user_id)
    if target:
        try:
            mod.unban_user(db, me, target)
            db.commit()
        except ValueError:
            db.rollback()
    return RedirectResponse(url="/web/mod/reports", status_code=303)
