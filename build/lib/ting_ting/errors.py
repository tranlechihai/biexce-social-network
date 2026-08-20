"""Shared API error envelope and exception handlers.

All API failures use a single JSON shape:

.. code-block:: json

   {"error": {"code": "string", "message": "string", "details": optional}}

Codes map to predictable statuses:

* ``validation`` → 422
* ``unauthenticated`` → 401
* ``forbidden`` → 403
* ``conflict`` → 409
* ``not_found`` → 404
"""

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


# ---------------------------------------------------------------------------
# Error envelope builder
# ---------------------------------------------------------------------------

def error_response(code: str, message: str, http_status: int, details=None) -> JSONResponse:
    """Build a standardised JSON error response."""
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=http_status, content=body)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

def register_error_handlers(app: FastAPI) -> None:
    """Wire global exception handlers into the FastAPI app."""

    @app.exception_handler(HTTPException)
    async def _fastapi_http_exception_handler(
        request: Request, exc: HTTPException
    ):
        """Handle application-level HTTPExceptions.

        fastapi.HTTPException subclasses StarletteHTTPException, so this
        more-specific handler takes precedence for app-raised exceptions.
        """
        detail = exc.detail
        # If detail is already our envelope dict, use it directly
        if isinstance(detail, dict) and "code" in detail:
            body = {"error": detail}
        else:
            code_map = {
                status.HTTP_401_UNAUTHORIZED: "unauthenticated",
                status.HTTP_403_FORBIDDEN: "forbidden",
                status.HTTP_404_NOT_FOUND: "not_found",
                status.HTTP_409_CONFLICT: "conflict",
            }
            code = code_map.get(exc.status_code, "error")
            body = {
                "error": {
                    "code": code,
                    "message": detail if isinstance(detail, str) else str(detail),
                }
            }
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        """Catch Starlette-level HTTPExceptions (e.g. 404 from unknown routes).

        This handler only fires for instances that are NOT fastapi.HTTPException
        (which has its own more-specific handler above). Covers unknown-route 404s.
        """
        code_map = {
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_401_UNAUTHORIZED: "unauthenticated",
            status.HTTP_403_FORBIDDEN: "forbidden",
        }
        code = code_map.get(exc.status_code, "error")
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
        return error_response(
            code=code,
            message=message,
            http_status=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ):
        """Override FastAPI's default 422 response with our envelope."""
        return error_response(
            code="validation",
            message="Request validation failed.",
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(request: Request, exc: Exception):
        return error_response(
            code="internal",
            message="An unexpected server error occurred.",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
