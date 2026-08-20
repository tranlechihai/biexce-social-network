"""Password hashing, JWT token management, and FastAPI auth dependencies."""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import APIKeyCookie, APIKeyHeader
from jose import JWTError, jwt as jose_jwt
from sqlalchemy.orm import Session

from ting_ting.config import Settings, get_settings
from ting_ting.database import get_db
from ting_ting.models import User


# ---------------------------------------------------------------------------
# Password hashing (bcrypt — adaptive, salted, constant-time)
# ---------------------------------------------------------------------------

import bcrypt as _bcrypt


def hash_password(password: str) -> str:
    """Hash a plain-text password with bcrypt (includes salt)."""
    return _bcrypt.hashpw(
        password.encode("utf-8"),
        _bcrypt.gensalt(),
    ).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return _bcrypt.checkpw(
        plain.encode("utf-8"),
        hashed.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

_JWT_COOKIE_NAME = "ting_ting_auth"

_security_header = APIKeyHeader(name="Authorization", auto_error=False)
_security_cookie = APIKeyCookie(name=_JWT_COOKIE_NAME, auto_error=False)


def create_token(user_id: int, username: str, session_id: str,
                 settings: Settings) -> str:
    """Create a short-lived signed JWT containing identity, session id, expiry."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes,
    )
    payload = {
        "sub": str(user_id),
        "username": username,
        "sid": session_id,
        "exp": expire,
    }
    return jose_jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str, settings: Settings) -> dict:
    """Decode and validate a JWT. Raises ``JWTError`` on failure."""
    return jose_jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )


def _extract_token(header: str | None) -> str | None:
    """Parse Bearer token from Authorization header."""
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

class CurrentUser(User):
    """Wrapper to distinguish auth'd user from raw model in type hints."""

    pass  # inherits all attributes from User


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Extract and validate the current user from Bearer header or cookie.

    Raises HTTP 401 for missing, malformed, or expired credentials.
    Raises HTTP 404 for tokens referencing a deleted user.
    """
    # 1) Try Bearer header
    auth_header = request.headers.get("Authorization")
    token = _extract_token(auth_header)

    # 2) Fall back to cookie
    if token is None:
        token = request.cookies.get(_JWT_COOKIE_NAME)

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_detail("unauthenticated", "No valid authentication provided."),
        )

    # 3) Decode & validate
    try:
        payload = decode_token(token, settings)
        user_id: int = int(payload["sub"])
        session_id: str = payload["sid"]
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_detail(
                "invalid_token", "The supplied token is invalid or expired."
            ),
        ) from None

    # 4) Look up user
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_detail(
                "invalid_token", "The authenticated user no longer exists."
            ),
        )

    # 5) Validate the server-side session (logout / revocation / expiry).
    from ting_ting import sessions as session_service
    if session_service.get_active_session(db, session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_detail(
                "session_expired",
                "This session is no longer active. Sign in again.",
            ),
        )
    # Expose to later handlers on the same request (logout, refresh, ...).
    request.state.sid = session_id

    if user.banned_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_detail(
                "banned", "This account has been suspended by a moderator."
            ),
        )
    return user


def get_current_user_web(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Web-aware auth dependency: raises *web-safe* exception for redirect.

    When the user is not authenticated this raises
    :class:`WebAuthRedirect` (a plain exception, NOT ``HTTPException``) so
    that a web-router exception handler can convert it to a redirect while the
    global API error handlers remain untouched.

    Use on Jinja2-rendered web routes.  Keep :func:`get_current_user` on
    ``/api/`` routes so they continue returning JSON 401.
    """
    # 1) Try Bearer header
    auth_header = request.headers.get("Authorization")
    token = _extract_token(auth_header)

    # 2) Fall back to cookie
    if token is None:
        token = request.cookies.get(_JWT_COOKIE_NAME)

    if token is None:
        raise WebAuthRedirect()

    # 3) Decode & validate
    try:
        payload = decode_token(token, settings)
        user_id: int = int(payload["sub"])
        session_id: str = payload["sid"]
    except (JWTError, KeyError, ValueError):
        raise WebAuthRedirect() from None

    # 4) Look up user
    user = db.get(User, user_id)
    if user is None:
        raise WebAuthRedirect()

    # 5) Revoked/expired session behaves like a logout on the web.
    from ting_ting import sessions as session_service
    if session_service.get_active_session(db, session_id) is None:
        raise WebAuthRedirect()
    request.state.sid = session_id
    if user.banned_at is not None:
        raise WebBanned()
    return user


class WebAuthRedirect(Exception):
    """Raised by :func:`get_current_user_web` when user is not authenticated.

    The web router's exception handler converts this into a redirect to
    ``/web/login``.
    """


class WebBanned(Exception):
    """Raised by :func:`get_current_user_web` when the user is banned.

    The web handler renders the suspension notice page instead of a redirect.
    """


def set_auth_cookie(response: Response, token: str, secure: bool) -> None:
    """Set the auth cookie with HttpOnly and SameSite attributes."""
    response.set_cookie(
        key=_JWT_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=3600,  # 1 hour
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    """Remove the auth cookie so subsequent cookie-only requests are unauthenticated."""
    response.delete_cookie(
        key=_JWT_COOKIE_NAME,
        path="/",
    )


# ---------------------------------------------------------------------------
# Normalized inputs
# ---------------------------------------------------------------------------

def normalize_username(raw: str) -> str:
    """Lowercase and strip whitespace from username."""
    return raw.strip().lower()


def normalize_email(raw: str) -> str:
    """Lowercase and strip whitespace from email."""
    return raw.strip().lower()


def validate_email(email: str) -> bool:
    """Basic email format check (presence of @ and dot)."""
    if "@" not in email or "." not in email.split("@")[-1]:
        return False
    return True


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------

def _error_detail(code: str, message: str) -> dict:
    return {"code": code, "message": message}
