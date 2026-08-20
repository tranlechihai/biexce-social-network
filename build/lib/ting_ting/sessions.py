"""Server-side session lifecycle — create, validate, revoke.

The JWT stays stateless and short-lived; the ``sessions`` table is the
single source of truth for whether a login is still alive.  A request is
authentic only when its token decodes AND its session row is present,
unrevoked, and unexpired.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from ting_ting.config import Settings, get_settings
from ting_ting.models import AuthSession


def new_session_id() -> str:
    return uuid.uuid4().hex


def create_session(
    db: DBSession,
    user_id: int,
    settings: Settings | None = None,
) -> AuthSession:
    """Open a new session for ``user_id`` and return the row."""
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    session = AuthSession(
        id=new_session_id(),
        user_id=user_id,
        expires_at=now + timedelta(days=settings.session_expire_days),
    )
    db.add(session)
    db.flush()
    return session


def get_active_session(
    db: DBSession,
    session_id: str,
) -> AuthSession | None:
    """Return the session row only if it is present, unrevoked, unexpired."""
    row = db.get(AuthSession, session_id)
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        return None
    return row


def revoke_session(db: DBSession, session_id: str) -> bool:
    """Revoke one session. Returns True if it was alive and is now dead."""
    row = db.get(AuthSession, session_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return True


def revoke_all_sessions(
    db: DBSession,
    user_id: int,
    keep_session_id: str | None = None,
) -> int:
    """Revoke every live session of the user (optionally keeping one).

    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
    ).all()
    count = 0
    for row in rows:
        if keep_session_id is not None and row.id == keep_session_id:
            continue
        row.revoked_at = now
        count += 1
    if count:
        db.flush()
    return count
