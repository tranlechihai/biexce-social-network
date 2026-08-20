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

# Must stay in sync with ting_ting.auth._JWT_COOKIE_NAME.
AUTH_COOKIE_NAME = "ting_ting_auth"


# ---------------------------------------------------------------------------
# API path identity (T-020)
# ---------------------------------------------------------------------------

def normalize_api_path(path: str) -> str:
    """Map versioned API paths onto the unversioned identity.

    ``/api/v1/auth/login`` and ``/api/auth/login`` are the SAME operation
    (dual-mounted router); rate-limit quotas, limiter keys, login-failure
    metrics and every other path-based classification must treat them
    identically so the deprecated alias cannot be used to evade limits.
    """
    if path == "/api/v1":
        return "/api"
    if path.startswith("/api/v1/"):
        return "/api/" + path[len("/api/v1/"):]
    return path


def api_cookie_csrf_violation(request: Request) -> bool:
    """True when an unsafe /api request authenticates via the browser auth
    cookie WITHOUT a valid double-submit CSRF token.

    Policy (T-020):
    * Bearer-authenticated requests (Authorization header) are exempt —
      cross-site requests cannot attach a custom header (CORS), so mobile /
      API clients are not affected.
    * Requests without the auth cookie are anonymous — nothing to protect.
    * Cookie-authenticated mutations must carry ``X-CSRF-Token`` matching the
      ``ting_ting_csrf`` cookie (issued by the CSRF response middleware).
    """
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return False
    path = request.url.path
    if path != "/api" and not path.startswith("/api/"):
        return False
    if request.headers.get("Authorization"):
        return False
    if not request.cookies.get(AUTH_COOKIE_NAME):
        return False
    expected = request.cookies.get(CSRF_COOKIE_NAME)
    supplied = request.headers.get(CSRF_HEADER_NAME)
    if not expected or not supplied or not hmac.compare_digest(expected, str(supplied)):
        return True
    return False


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
    """Return the per-minute limit for an abuse-sensitive request.

    Paths are normalized first so ``/api/v1/...`` receives exactly the same
    quotas as the (deprecated) ``/api/...`` alias.
    """
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    path = normalize_api_path(request.url.path)
    if path in {"/api/auth/register", "/web/register"}:
        return 10
    if path in {"/api/auth/login", "/web/login"}:
        return 20
    if path in {"/web/profile/password", "/api/auth/change-password"}:
        return 10
    if path in {"/web/posts/create", "/web/profile/update", "/web/avatar/upload"}:
        return 30
    if path.startswith("/api/") or path.startswith("/web/"):
        return 120
    return None
