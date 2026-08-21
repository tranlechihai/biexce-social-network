"""Authentication endpoints — register, login, logout, refresh, sessions."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting import refresh as refresh_service
from ting_ting import sessions as session_service
from ting_ting.auth import (
    REFRESH_COOKIE_NAME,
    clear_auth_cookie,
    clear_refresh_cookie,
    create_token,
    get_current_user,
    hash_password,
    normalize_email,
    normalize_username,
    set_auth_cookie,
    set_refresh_cookie,
    verify_password,
)
from ting_ting.config import Settings, get_settings
from ting_ting.database import get_db
from ting_ting.models import AuthSession, User
from ting_ting.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionItem,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
) -> UserResponse:
    """Create a new user account with unique username and email."""
    username = normalize_username(body.username)
    email = normalize_email(body.email)
    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation",
                "message": "Password exceeds 72 bytes (bcrypt limit).",
            },
        )

    new_user = User(
        username=username,
        email=email,
        password_hash=hash_password(body.password),
    )
    # T-023: identifiers under a fresh deletion tombstone (30-day window)
    # are not reusable yet.
    from ting_ting import account as account_service

    try:
        account_service.assert_credentials_available(db, username, email)
    except ValueError as exc:
        code = exc.args[0]
        if code in ("username_taken", "email_taken"):
            target = "username" if code == "username_taken" else "email"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "conflict",
                    "message": (
                        f"This {target} was recently deleted and stays "
                        f"reserved for 30 days."
                    ),
                },
            ) from None

    db.add(new_user)
    try:
        db.commit()
        db.refresh(new_user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict",
                "message": "A user with this username or email already exists.",
            },
        ) from None

    return UserResponse.model_validate(new_user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    resp: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Authenticate with username-or-email plus password.

    Returns a JWT access token and sets an HttpOnly/SameSite cookie.
    """
    # Look up by either username or email
    stmt = select(User).where(
        (User.username == normalize_username(body.identifier))
        | (User.email == normalize_email(body.identifier))
    )
    user = db.scalar(stmt)

    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthenticated",
                "message": "Invalid credentials.",
            },
        )

    if user.banned_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "banned",
                "message": "This account has been suspended by a moderator.",
            },
        )

    session = session_service.create_session(db, user.id, settings)
    refresh_value = refresh_service.issue_refresh_token(db, session, settings)
    token = create_token(user.id, user.username, session.id, settings)
    set_auth_cookie(resp, token, secure=settings.cookie_secure)
    set_refresh_cookie(resp, refresh_value, secure=settings.cookie_secure)
    db.commit()

    return TokenResponse(access_token=token, refresh_token=refresh_value)


@router.post("/logout")
async def logout(
    request: Request,
    resp: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Revoke the current session server-side and clear the cookie."""
    session_id = getattr(request.state, "sid", None)
    if session_id:
        session_service.revoke_session(db, session_id)
        db.commit()
    clear_auth_cookie(resp)
    clear_refresh_cookie(resp)
    return {"message": "Logged out successfully."}


@router.post("/logout-all")
async def logout_all(
    resp: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Revoke every session for this account, including the current one."""
    session_service.revoke_all_sessions(db, user.id)
    db.commit()
    clear_auth_cookie(resp)
    clear_refresh_cookie(resp)
    return {"message": "All sessions revoked."}


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    resp: Response,
    body: RefreshRequest | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Extend a still-active session.

    T-021:
    * With a refresh token (JSON body or browser cookie): works even while
      the access JWT is expired. The token is ROTATED — the presented value
      is invalidated and a successor is returned (JSON + cookie).
    * Without one: legacy behavior — re-mint from a still-valid access JWT
      (no refresh token is issued).

    Replaying a rotated token revokes its whole session and returns
    ``401 refresh_replay``.
    """
    raw_token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        request_id = getattr(request.state, "request_id", None)
        try:
            session, new_token = refresh_service.consume_refresh_token(
                db, raw_token, request_id=request_id, settings=settings,
            )
        except refresh_service.RefreshTokenError as exc:
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": exc.code, "message": exc.message},
            ) from None

        user = db.get(User, session.user_id)
        if user is None or user.banned_at is not None:
            if user is not None:
                session_service.revoke_session(db, session.id)
            db.commit()
            code, message = (
                ("unauthenticated", "Invalid credentials.")
                if user is None
                else ("banned", "This account has been suspended by a moderator.")
            )
            status_code = 401 if user is None else 403
            raise HTTPException(status_code=status_code, detail={"code": code, "message": message})

        db.commit()
        token = create_token(user.id, user.username, session.id, settings)
        set_auth_cookie(resp, token, secure=settings.cookie_secure)
        set_refresh_cookie(resp, new_token, secure=settings.cookie_secure)
        return TokenResponse(access_token=token, refresh_token=new_token)

    # Legacy path: re-mint from a still-valid access JWT (cookie or bearer).
    user = await get_current_user(request, db, settings)
    session_id = getattr(request.state, "sid", None)
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Token carries no session."},
        )
    token = create_token(user.id, user.username, session_id, settings)
    set_auth_cookie(resp, token, secure=settings.cookie_secure)
    return TokenResponse(access_token=token)


@router.get("/sessions", response_model=list[SessionItem])
async def list_sessions(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SessionItem]:
    """Active server-side sessions for this account; current one flagged."""
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.created_at.desc())
    ).all()
    current_sid = getattr(request.state, "sid", None)
    return [
        SessionItem(
            id=row.id, created_at=row.created_at, expires_at=row.expires_at,
            current=(row.id == current_sid),
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    request: Request,
    session_id: str,
    resp: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Revoke one of this account's sessions (including the current one)."""
    row = db.get(AuthSession, session_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Session not found."},
        )
    session_service.revoke_session(db, session_id)
    db.commit()
    if getattr(request.state, "sid", None) == session_id:
        clear_auth_cookie(resp)
        clear_refresh_cookie(resp)


@router.post("/change-password")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Rotate credentials; every other session is revoked.

    API parity with the web password-change form: current password required,
    new password validated (min length, not identical, bcrypt byte limit).
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Current password is incorrect."},
        )
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": "New password must be at least 8 characters."},
        )
    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": "New password is too long."},
        )
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation", "message": "New password must differ from the current one."},
        )

    user.password_hash = hash_password(body.new_password)
    current_sid = getattr(request.state, "sid", None)
    session_service.revoke_all_sessions(db, user.id, keep_session_id=current_sid)
    db.commit()
    return {"message": "Password changed."}
