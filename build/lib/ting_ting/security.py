"""Small runtime security controls shared by the web application."""

import hmac
import secrets
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status


CSRF_COOKIE_NAME = "ting_ting_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


async def require_csrf(request: Request) -> None:
    """Require a double-submit token for unsafe browser requests."""
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return

    expected = request.cookies.get(CSRF_COOKIE_NAME)
    supplied = request.headers.get(CSRF_HEADER_NAME)
    if supplied is None:
        form = await request.form()
        supplied = form.get(CSRF_FORM_FIELD)

    if not expected or not supplied or not hmac.compare_digest(expected, str(supplied)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Invalid CSRF token."},
        )


def csrf_token_for(request: Request) -> str:
    """Return the request token prepared by the CSRF response middleware."""
    return getattr(request.state, "csrf_token", None) or secrets.token_urlsafe(32)


class RateLimiter:
    """Thread-safe in-memory fixed-window limiter for a single app process."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(current)
            return True


rate_limiter = RateLimiter()


def request_rate_limit(request: Request) -> int | None:
    """Return the per-minute limit for an abuse-sensitive request."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    path = request.url.path
    if path in {"/api/auth/register", "/web/register"}:
        return 10
    if path in {"/api/auth/login", "/web/login"}:
        return 20
    if path == "/web/profile/password":
        return 10
    if path in {"/web/posts/create", "/web/profile/update", "/web/avatar/upload"}:
        return 30
    if path.startswith("/api/") or path.startswith("/web/"):
        return 120
    return None
