"""Authentication endpoints — register, login, logout."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting import sessions as session_service
from ting_ting.auth import (
    clear_auth_cookie,
    create_token,
    get_current_user,
    hash_password,
    normalize_email,
    normalize_username,
    set_auth_cookie,
    verify_password,
)
from ting_ting.config import Settings, get_settings
from ting_ting.database import get_db
from ting_ting.models import User
from ting_ting.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
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

    new_user = User(
        username=username,
        email=email,
        password_hash=hash_password(body.password),
    )
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
    token = create_token(user.id, user.username, session.id, settings)
    set_auth_cookie(resp, token, secure=settings.cookie_secure)
    db.commit()

    return TokenResponse(access_token=token)


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
    return {"message": "All sessions revoked."}


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    resp: Response,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Mint a fresh short-lived JWT for the still-active session.

    The session itself is unchanged — this only extends access.
    """
    session_id = getattr(request.state, "sid", None)
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Token carries no session."},
        )
    token = create_token(user.id, user.username, session_id, settings)
    set_auth_cookie(resp, token, secure=settings.cookie_secure)
    return TokenResponse(access_token=token)


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
