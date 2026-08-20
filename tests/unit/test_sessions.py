"""Unit tests for Increment 6 — server-side session lifecycle service."""

from datetime import datetime, timedelta, timezone

import pytest

from ting_ting import sessions as svc
from ting_ting.auth import hash_password
from ting_ting.models import User


@pytest.fixture
def user(tmp_session):
    u = User(
        username="sess_a",
        email="sess_a@unit.com",
        password_hash=hash_password("pass1234"),
    )
    tmp_session.add(u)
    tmp_session.commit()
    tmp_session.refresh(u)
    return u


class TestCreate:
    def test_create_returns_live_session(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        assert s.id and s.user_id == user.id
        assert s.revoked_at is None
        assert s.expires_at > datetime.now(timezone.utc)

    def test_expiry_uses_settings(self, tmp_session, user):
        from ting_ting.config import Settings
        s = svc.create_session(
            tmp_session, user.id,
            Settings(jwt_secret="x", session_expire_days=3),
        )
        span = s.expires_at - datetime.now(timezone.utc)
        assert timedelta(days=2, hours=23) < span <= timedelta(days=3)


class TestGetActive:
    def test_active_session_returned(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        assert svc.get_active_session(tmp_session, s.id) is not None

    def test_revoked_session_none(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        svc.revoke_session(tmp_session, s.id)
        assert svc.get_active_session(tmp_session, s.id) is None

    def test_expired_session_none(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        tmp_session.commit()
        assert svc.get_active_session(tmp_session, s.id) is None

    def test_unknown_session_none(self, tmp_session):
        assert svc.get_active_session(tmp_session, "doesnotexist") is None


class TestRevoke:
    def test_revoke_sets_timestamp(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        assert svc.revoke_session(tmp_session, s.id) is True
        assert s.revoked_at is not None

    def test_revoke_idempotent_false(self, tmp_session, user):
        s = svc.create_session(tmp_session, user.id)
        svc.revoke_session(tmp_session, s.id)
        assert svc.revoke_session(tmp_session, s.id) is False

    def test_revoke_missing_false(self, tmp_session):
        assert svc.revoke_session(tmp_session, "ghost") is False


class TestRevokeAll:
    def test_revoke_all_kills_every_session(self, tmp_session, user):
        a = svc.create_session(tmp_session, user.id)
        b = svc.create_session(tmp_session, user.id)
        count = svc.revoke_all_sessions(tmp_session, user.id)
        tmp_session.commit()
        assert count == 2
        assert svc.get_active_session(tmp_session, a.id) is None
        assert svc.get_active_session(tmp_session, b.id) is None

    def test_revoke_all_keeps_current(self, tmp_session, user):
        a = svc.create_session(tmp_session, user.id)
        b = svc.create_session(tmp_session, user.id)
        count = svc.revoke_all_sessions(
            tmp_session, user.id, keep_session_id=a.id,
        )
        tmp_session.commit()
        assert count == 1
        assert svc.get_active_session(tmp_session, a.id) is not None
        assert svc.get_active_session(tmp_session, b.id) is None

    def test_revoke_all_scoped_to_user(self, tmp_session, user):
        other = User(
            username="sess_b",
            email="sess_b@unit.com",
            password_hash=hash_password("pass1234"),
        )
        tmp_session.add(other)
        tmp_session.commit()
        tmp_session.refresh(other)
        a = svc.create_session(tmp_session, user.id)
        b = svc.create_session(tmp_session, other.id)
        svc.revoke_all_sessions(tmp_session, user.id)
        tmp_session.commit()
        assert svc.get_active_session(tmp_session, a.id) is None
        assert svc.get_active_session(tmp_session, b.id) is not None
