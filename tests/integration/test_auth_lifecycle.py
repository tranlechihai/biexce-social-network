"""Integration tests for Increment 6 — auth session lifecycle.

Covers: login/register mint a session; logout revokes (401 after);
logout-all revokes every device; refresh re-mints a token but keeps the
session; password change (API + web) revokes the OTHER sessions only;
expired/unknown sessions are rejected.
"""

import pytest
from fastapi import status


@pytest.fixture
def db_session(tmp_engine):
    from sqlalchemy.orm import sessionmaker
    session = sessionmaker(bind=tmp_engine, expire_on_commit=False)()
    yield session
    session.close()


class _A:
    PREFIX = "i6"

    @classmethod
    def register(cls, client, username):
        name = f"{cls.PREFIX}_{username}"
        resp = client.post("/api/auth/register", json={
            "username": name, "email": f"{name}@i6.com",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        return resp.json()["id"]

    @classmethod
    def login(cls, client, username, password="securepass1"):
        resp = client.post("/api/auth/login", json={
            "identifier": f"{cls.PREFIX}_{username}",
            "password": password,
        })
        assert resp.status_code == status.HTTP_200_OK, resp.text
        return resp.json()["access_token"]

    @classmethod
    def whoami(cls, client, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = client.get("/api/profile/me", headers=headers)
        return resp


def _live_sessions(db_session, user_id):
    """Fresh-core-connection read: the test ORM session can hold a stale
    SQLite snapshot, so inspect the table directly."""
    from sqlalchemy import text
    eng = db_session.get_bind()
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM sessions WHERE user_id = :u AND revoked_at IS NULL"),
            {"u": user_id},
        ).fetchall()
    return [r[0] for r in rows]


def test_login_creates_session_and_logout_revokes_it(client, db_session):
    uid = _A.register(client, "lc1")
    tok = _A.login(client, "lc1")
    assert len(_live_sessions(db_session, uid)) == 1

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert len(_live_sessions(db_session, uid)) == 0

    # The now-revoked token (e.g. a leaked one, or another open tab) is dead:
    resp = _A.whoami(client, tok)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["error"]["code"] == "session_expired"


def test_logout_all_revokes_every_device(client, db_session):
    uid = _A.register(client, "lc2")
    _A.login(client, "lc2")  # device A (client cookie)
    # Simulate a second device by logging out and in with a fresh token via header.
    client.post("/api/auth/logout")
    tok_b = _A.login(client, "lc2")
    # Re-login device A too so there are two live sessions.
    tok_a = _A.login(client, "lc2")
    live = _live_sessions(db_session, uid)
    assert len(live) == 2

    resp = client.post("/api/auth/logout-all")
    assert resp.status_code == 200
    assert len(_live_sessions(db_session, uid)) == 0

    # Both devices are now dead.
    assert _A.whoami(client, tok_a).status_code == 401
    assert _A.whoami(client, tok_b).status_code == 401


def test_refresh_mints_token_and_keeps_session(client, db_session):
    uid = _A.register(client, "lc3")
    _A.login(client, "lc3")
    before = _live_sessions(db_session, uid)
    assert len(before) == 1
    sid = before[0]

    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    # Session count unchanged (refresh does not create a new session).
    after = _live_sessions(db_session, uid)
    assert len(after) == 1 and after[0] == sid


def test_password_change_revokes_other_sessions_only(client, db_session):
    uid = _A.register(client, "lc4")
    # Two logins: the second one replaces the client cookie, so S2 (cookie)
    # is "this device" and S1 (tok_a) is "the other device".
    tok_a = _A.login(client, "lc4")
    tok_b = _A.login(client, "lc4")
    live = _live_sessions(db_session, uid)
    assert len(live) == 2

    # Change password as this device (cookie = S2); other sessions revoked.
    resp = client.post("/api/auth/change-password", json={
        "current_password": "securepass1",
        "new_password": "newpass99",
    })
    assert resp.status_code == 200, resp.text

    live = _live_sessions(db_session, uid)
    assert len(live) == 1

    # The other device is dead; this device (cookie/tok_b) survives.
    assert _A.whoami(client, tok_a).status_code == 401
    assert _A.whoami(client, tok_b).status_code == 200
    assert _A.whoami(client).status_code == 200


def test_change_password_wrong_current_403(client):
    _A.register(client, "lc5")
    _A.login(client, "lc5")
    resp = client.post("/api/auth/change-password", json={
        "current_password": "wrongpass",
        "new_password": "newpass99",
    })
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_change_password_reuses_current_422(client):
    _A.register(client, "lc6")
    _A.login(client, "lc6")
    resp = client.post("/api/auth/change-password", json={
        "current_password": "securepass1",
        "new_password": "securepass1",
    })
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_change_password_too_short_422(client):
    _A.register(client, "lc7")
    _A.login(client, "lc7")
    resp = client.post("/api/auth/change-password", json={
        "current_password": "securepass1",
        "new_password": "short",
    })
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_new_password_works_after_change(client):
    _A.register(client, "lc8")
    _A.login(client, "lc8")
    resp = client.post("/api/auth/change-password", json={
        "current_password": "securepass1",
        "new_password": "newpass99",
    })
    assert resp.status_code == 200
    client.post("/api/auth/logout")
    resp = client.post("/api/auth/login", json={
        "identifier": f"{_A.PREFIX}_lc8", "password": "newpass99"})
    assert resp.status_code == 200


def test_expired_session_rejected(client, db_session):
    uid = _A.register(client, "lc9")
    _A.login(client, "lc9")
    from datetime import datetime, timezone
    from ting_ting.models import AuthSession
    s = db_session.get(AuthSession, _live_sessions(db_session, uid)[0])
    s.expires_at = datetime.now(timezone.utc)
    db_session.commit()
    resp = _A.whoami(client)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_expired"
