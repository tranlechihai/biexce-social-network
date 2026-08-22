"""Shared user role and active-ban predicates.

Keep Python authentication checks and SQL visibility filters on the same
definition so an expired temporary ban cannot remain permanent on one surface.
"""

from datetime import datetime, timezone

from sqlalchemy import and_, or_

from ting_ting.models import User

STAFF_ROLES = frozenset({"moderator", "admin"})
ROLE_RANK = {"user": 0, "moderator": 1, "admin": 2}


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_staff(user: User) -> bool:
    return user.role in STAFF_ROLES


def is_actively_banned(user: User, now: datetime | None = None) -> bool:
    if user.banned_at is None:
        return False
    if user.banned_until is None:
        return True
    boundary = now or datetime.now(timezone.utc)
    if boundary.tzinfo is not None:
        boundary = boundary.astimezone(timezone.utc).replace(tzinfo=None)
    until = user.banned_until
    if until.tzinfo is not None:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    return until > boundary


def active_ban_clause(now: datetime | None = None):
    boundary = now or utc_now_naive()
    if boundary.tzinfo is not None:
        boundary = boundary.astimezone(timezone.utc).replace(tzinfo=None)
    return and_(
        User.banned_at.is_not(None),
        or_(User.banned_until.is_(None), User.banned_until > boundary),
    )


def not_actively_banned_clause(now: datetime | None = None):
    boundary = now or utc_now_naive()
    if boundary.tzinfo is not None:
        boundary = boundary.astimezone(timezone.utc).replace(tzinfo=None)
    return or_(
        User.banned_at.is_(None),
        and_(User.banned_until.is_not(None), User.banned_until <= boundary),
    )
