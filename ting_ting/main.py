"""FastAPI application factory.

Entry point used by ``uvicorn ting_ting.main:app``.
"""

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import func, select

from ting_ting import metrics as _metrics
from ting_ting.database import get_engine, get_session_factory, validate_and_initialize_schema
from ting_ting.auth import WebAuthRedirect, WebBanned, decode_token
from ting_ting import notifications as notification_service
from ting_ting.errors import register_error_handlers
from ting_ting.api import (
    account, auth, discovery, extensions, interactions, moderation, notifications,
    posts, profile, social, users,
)
from ting_ting.media import router as media_router
from ting_ting.security import (
    CSRF_COOKIE_NAME,
    api_cookie_csrf_violation,
    normalize_api_path,
    rate_limiter,
    request_rate_limit,
)
from ting_ting.config import get_settings

# Baseline CSP matched to the current Jinja2 templates (inline <script> and
# style attributes are in use, so those two need 'unsafe-inline').  Tighten
# with nonces when the inline JS moves to external files.
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)

app = FastAPI(
    title="Biexce Social",
    version="0.1.0",
)


def _configure_access_log() -> logging.Logger:
    logger = logging.getLogger("ting_ting.access")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s INFO "
            "rid=%(request_id)s %(method)s %(path)s "
            "-> %(status)s %(duration_ms).2f ms"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


_access_log = _configure_access_log()

# Error envelope handler
register_error_handlers(app)

# Web auth redirect: unauthenticated web routes redirect to login instead of
# returning JSON 401 (API routes keep JSON behavior via get_current_user).
@app.exception_handler(WebAuthRedirect)
async def _web_auth_redirect_handler(_request: Request, _exc: WebAuthRedirect):
    """Redirect unauthenticated web page request to /web/login."""
    return RedirectResponse(url="/web/login", status_code=302)

from fastapi.responses import HTMLResponse  # noqa: E402


@app.exception_handler(WebBanned)
async def _web_banned_handler(_request: Request, _exc: WebBanned):
    """Banned users get a suspension notice page instead of app content."""
    return HTMLResponse(
        """<!doctype html><html><head><meta charset="utf-8">
<title>Tài khoản tạm khóa</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;
font-family:system-ui,sans-serif;background:#090909;color:#f7f7f7">
<main style="max-width:420px;padding:28px;border:1px solid #282828;
border-radius:17px;background:#0d0d0d;text-align:center">
<h1 style="font-size:1.3rem;margin:0 0 12px">Tài khoản đã bị tạm khóa</h1>
<p style="color:#8c8c92;font-size:.9rem;line-height:1.6;margin:0 0 18px">
Tài khoản của bạn đã bị khóa bởi một điều phối viên do vi phạm quy tắc
cộng đồng. Vui lòng liên hệ quản trị viên nếu bạn cho rằng đây là nhầm lẫn.</p>
<a href="/web/login" style="color:#ff5d3a;font-weight:700;font-size:.85rem">
Đăng nhập tài khoản khác</a></main></body></html>""",
        status_code=403,
    )

# Root redirect — send browser users to the web demo
@app.get("/")
async def _root_redirect():
    """Redirect top-level / to the /web demo namespace."""
    return RedirectResponse(url="/web/feed", status_code=302)


# ---------------------------------------------------------------------------
# Observability — liveness / readiness / metrics
# ---------------------------------------------------------------------------

@app.get("/health")
async def _health():
    """Liveness: process up and able to serve. No dependency checks."""
    return {"status": "ok"}


from sqlalchemy import text as _sa_text  # noqa: E402


@app.get("/ready")
async def _ready():
    """Readiness: the database must answer before we take real traffic."""
    try:
        with get_engine().connect() as conn:
            conn.execute(_sa_text("SELECT 1"))
    except Exception:
        return JSONResponse(
            {"status": "not_ready", "database": "unavailable"},
            status_code=503,
        )
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
async def _metrics_endpoint():
    """Prometheus text exposition of in-process counters."""
    return _metrics.render_prometheus()

# API Routers — versioned dual-mount:
#   /api/v1/...  canonical (what new clients — mobile/app — must use)
#   /api/...     legacy alias, DEPRECATED: responses carry Deprecation/Warning
#                headers. Remove the legacy prefix in the next major version.
API_ROUTERS = [
    auth.router, profile.router, social.router, account.router,
    posts.router, posts.feed_router, interactions.router,
    extensions.profile_router, extensions.social_router,
    extensions.activity_router, extensions.feature_router,
    notifications.router, users.router, moderation.router,
    discovery.router,
]
for _api_router in API_ROUTERS:
    app.include_router(_api_router, prefix="/api")
    app.include_router(_api_router, prefix="/api/v1")
app.include_router(media_router)  # file delivery is not versioned


@app.middleware("http")
async def _api_deprecation_notice(request: Request, call_next):
    """Mark legacy /api/... responses as deprecated (v1 stays clean)."""
    path = request.scope.get("path", "")
    response = await call_next(request)
    if path.startswith("/api/") and not path.startswith("/api/v1"):
        response.headers["Deprecation"] = "true"
        response.headers["Warning"] = (
            '299 - ""The unversioned /api path is deprecated; use /api/v1."'
        )
    return response

# Web routes (Jinja2 templates) — namespaced under /web to avoid colliding
# with /api routes and to keep the browser demo cleanly separated from JSON API.
from ting_ting.web.routes import router as web_router  # noqa: E402
app.include_router(web_router, prefix="/web")

# Static files
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.middleware("http")
async def _notification_badge(request: Request, call_next):
    """Attach the authenticated user's unread-notification count to the
    request so templates can render a badge.  Never breaks page rendering."""
    count = 0
    token = request.cookies.get("ting_ting_auth")
    if request.url.path.startswith("/web/") and token:
        try:
            from ting_ting.config import get_settings
            from ting_ting.models import Report, User
            payload = decode_token(token, get_settings())
            user_id = int(payload["sub"])
            session = get_session_factory()()
            try:
                count = notification_service.unread_count(session, user_id)
                user = session.get(User, user_id)
                if user is not None:
                    request.state.username = user.username
                    request.state.user_is_moderator = bool(user.is_moderator)
                    if user.is_moderator:
                        request.state.pending_reports = int(
                            session.scalar(
                                select(func.count()).select_from(Report).where(
                                    Report.status == "pending",
                                )
                            )
                            or 0
                        )
            finally:
                session.close()
        except Exception:
            count = 0
    request.state.unread_notifications = count
    return await call_next(request)


@app.middleware("http")
async def _security_controls(request: Request, call_next):
    """Issue CSRF cookies, protect cookie-authed API mutations, and bound
    abuse-sensitive mutation rates."""
    settings = get_settings()

    # T-020: cookie-authenticated /api mutations require a valid CSRF token
    # (Bearer requests are exempt — browsers cannot attach custom headers
    # cross-site).  Must run before rate limiting so probes don't consume quota.
    if api_cookie_csrf_violation(request):
        from ting_ting.errors import error_response
        return error_response(
            "forbidden",
            "A valid CSRF token is required for cookie-authenticated API requests.",
            403,
        )

    limit = request_rate_limit(request) if settings.rate_limit_enabled else None
    if limit is not None:
        client = request.client.host if request.client else "unknown"
        # Normalized identity so /api and /api/v1 share one quota bucket.
        norm_path = normalize_api_path(request.url.path)
        key = f"{client}:{request.method}:{norm_path}"
        if not rate_limiter.allow(key, limit=limit, window_seconds=60):
            from ting_ting.errors import error_response
            return error_response(
                "rate_limited", "Too many requests. Try again later.", 429,
            )

    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        import secrets
        token = secrets.token_urlsafe(32)
    request.state.csrf_token = token
    response = await call_next(request)
    if not request.cookies.get(CSRF_COOKIE_NAME):
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=False,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# Outermost middleware (registered last): request ID, metrics, access log.
@app.middleware("http")
async def _request_id_and_metrics(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or _metrics.new_request_id()
    request.state.request_id = request_id
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _metrics.inc_requests("5xx")
        _metrics.observe_latency_ms((time.perf_counter() - start) * 1000)
        raise
    _metrics.inc_requests(_metrics.status_class_of(response.status_code))
    duration_ms = (time.perf_counter() - start) * 1000
    _metrics.observe_latency_ms(duration_ms)
    _metrics.observe_request(duration_ms / 1000.0)
    if (
        request.method == "POST"
        # Normalized identity: counts /api/auth/login and /api/v1/auth/login.
        and normalize_api_path(request.url.path) == "/api/auth/login"
        and response.status_code >= 400
    ):
        _metrics.inc("auth_login_failures_total")
    _access_log.info(
        "",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


# Lifecycle — validate & initialize schema on startup
@app.on_event("startup")
async def _startup():
    validate_and_initialize_schema()


# ---------------------------------------------------------------------------
# Convenience entry point for CLI usage (``python -m ting_ting``)
# ---------------------------------------------------------------------------

def main():
    """Validate & initialize schema, then print configuration summary."""
    validate_and_initialize_schema()
    from ting_ting.config import get_settings
    s = get_settings()
    print(f"Database: {s._redact(s.database_url)}")
    print(f"JWT algorithm: {s.jwt_algorithm}")
    print(f"JWT expiry (min): {s.jwt_expire_minutes}")
    print(f"Cookie secure: {s.cookie_secure}")
    print("Schema validation & initialization passed.")


if __name__ == "__main__":
    main()
