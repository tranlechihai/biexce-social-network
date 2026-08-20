"""Opaque rotating refresh tokens (T-021).

Design:
* The client-facing token is a 32-byte URL-safe random value.
* The database stores only its SHA-256 hex digest (a DB leak is not a
  credential leak).
* A token is bound to one server-side session. Validation = row exists,
  unexpired, and its session is still active.
* Every successful use ROTATES the token: the old row is revoked and
  points at its successor via ``replaced_by_id``.
* Re-presenting a rotated token is a REPLAY (token theft). The whole
  session is revoked so neither the stolen copy nor the victim's chain
  can survive; the event is logged and counted.

Refresh tokens do not extend session lifetime — they expire together with
their session.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from ting_ting import metrics
from ting_ting.config import Settings
from ting_ting.models import AuthSession, RefreshToken
from ting_ting import sessions as session_service


_logger = logging.getLogger("ting_ting.security")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token(
    db: DBSession,
    session: AuthSession,
    settings: Settings | None = None,
) -> str:
    """Create a refresh token for ``session`` and return the opaque value."""
    token = secrets.token_urlsafe(32)
    row = RefreshToken(
        session_id=session.id,
        user_id=session.user_id,
        token_hash=_hash_token(token),
        expires_at=session.expires_at,
    )
    db.add(row)
    db.flush()
    return token


class RefreshTokenError(Exception):
    """Refresh-token flow failure with a client-safe error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def consume_refresh_token(
    db: DBSession,
    token: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[AuthSession, str]:
    """Validate and rotate a refresh token.

    Returns ``(session, new_token_value)``. Raises
    :class:`RefreshTokenError` with a stable error code on any failure:

    * ``invalid_refresh`` — unknown or dead (not rotated) token.
    * ``refresh_expired`` — token lifetime ended.
    * ``session_expired`` — the bound session was revoked/expired.
    * ``refresh_replay`` — a rotated token was re-presented; the session
      was revoked as the security response.
    """
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(token)))
    if row is None:
        raise RefreshTokenError("invalid_refresh", "The refresh token is invalid.")

    session = session_service.get_active_session(db, row.session_id)

    if row.revoked_at is not None:
        if row.replaced_by_id is not None:
            # Replay of an already-rotated token: kill the session (the
            # successor dies too) and record the security event.
            if session is not None:
                session_service.revoke_session(db, row.session_id)
            metrics.inc("auth_refresh_replays_total")
            _logger.warning(
                "rid=%s refresh token replay: user=%s session=%s (session revoked)",
                request_id or "-", row.user_id, row.session_id,
            )
            raise RefreshTokenError(
                "refresh_replay",
                "A refresh token was reused. All sessions for this sign-in were revoked.",
            )
        raise RefreshTokenError("invalid_refresh", "The refresh token is invalid.")

    if session is None:
        raise RefreshTokenError("session_expired", "This session is no longer active.")

    if row.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise RefreshTokenError("refresh_expired", "The refresh token has expired. Sign in again.")

    now = datetime.now(timezone.utc)
    row.revoked_at = now
    db.flush()

    new_token = secrets.token_urlsafe(32)
    successor = RefreshToken(
        session_id=row.session_id,
        user_id=row.user_id,
        token_hash=_hash_token(new_token),
        expires_at=session.expires_at,
    )
    db.add(successor)
    db.flush()
    row.replaced_by_id = successor.id
    db.flush()
    return session, new_token


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison helper (tests / parity checks)."""
    return hmac.compare_digest(a, b)
