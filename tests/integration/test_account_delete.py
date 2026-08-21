"""Integration tests for T-023 (part 2) — account DELETION (irreversible).

Covers:
* POST /account/delete — password gate, auth;
* hard delete — user row, content, graph rows both directions, sessions/
  refresh tokens, media files;
* deletion tombstone — 30-day username/email reservation, expired window
  releases the identifiers;
* moderation evidence — reports surviving deletion anonymized (user refs ->
  NULL), content pins -> NULL;
* evidence retention — reports past the 30-day window hidden from the queue
  and 404 on resolve;
* web surface — /web/account/delete wrong password re-render + success 303.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

PNG_OK = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x00" * 400  # valid 1.2 KB PNG


def _app():
    from ting_ting.main import app

    return app


def _login(client: TestClient, name: str, password: str = "securepass1") -> None:
    client.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t23d.com", "password": password,
    })
    resp = client.post("/api/auth/login", json={"identifier": name, "password": password})
    assert resp.status_code == 200, resp.text


def _post(client: TestClient, content: str = "hi") -> int:
    resp = _mutate(client, "POST", "/api/posts", json={"content": content, "audience": "PUBLIC"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_moderator(client: TestClient, tmp_session, username: str) -> None:
    from ting_ting.models import User

    client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@t23d.com", "password": "securepass1",
    })
    row = tmp_session.query(User).filter(User.username == username).first()
    row.is_moderator = True
    tmp_session.commit()
    _login(client, username)


def _user_exists(username: str, engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE username = :u"),
            {"u": username},
        ).scalar()
    return bool(row)


def _tombstone(engine, username: str) -> tuple | None:
    """Fresh-connection read of the deletion tombstone (WAL snapshot caveat)."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT username, email, deleted_at FROM deleted_accounts "
                "WHERE username = :u"
            ),
            {"u": username},
        ).one_or_none()


def _backdate_tombstone(engine, username: str, days: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE deleted_accounts SET deleted_at = :ts WHERE username = :u"),
            {"ts": ts, "u": username},
        )


def _backdate_report(engine, report_id: int, days: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reports SET created_at = :ts WHERE id = :rid"),
            {"ts": ts, "rid": report_id},
        )


def _csrf_headers(c: TestClient) -> dict:
    tok = c.cookies.get("ting_ting_csrf")
    return {"X-CSRF-Token": tok} if tok else {}


def _mutate(c: TestClient, method: str, url: str, **kw):
    headers = dict(kw.pop("headers", None) or {})
    headers.update(_csrf_headers(c))
    if headers:
        kw["headers"] = headers
    return c.request(method, url, **kw)


# ---------------------------------------------------------------------------
# Password / auth gate
# ---------------------------------------------------------------------------

class TestDeleteGate:
    def test_delete_requires_auth(self, client):
        assert client.post("/api/account/delete",
                           json={"password": "x"}).status_code == 401

    def test_delete_requires_correct_password(self, client, tmp_session, tmp_engine):
        c = TestClient(_app())
        _login(c, "t23d_pw")
        resp = _mutate(c, "POST", "/api/account/delete", json={"password": "wrongpass99"})
        assert resp.status_code == 403, resp.text
        assert _user_exists("t23d_pw", tmp_engine)
        assert _tombstone(tmp_engine, "t23d_pw") is None


# ---------------------------------------------------------------------------
# Hard delete effects
# ---------------------------------------------------------------------------

class TestDeleteEffects:
    def test_delete_removes_account_content_and_relations(
        self, client, tmp_session, tmp_engine,
    ):
        a = TestClient(_app())
        _login(a, "t23d_victim")
        pid = _post(a, "victim post")

        b = TestClient(_app())
        _login(b, "t23d_friend")
        b_pid = _post(b, "friend post")
        victim_id = a.get("/api/users/t23d_victim").json()["id"]
        assert _mutate(b, "PUT", f"/api/social/follows/{victim_id}").status_code == 200
        assert _mutate(b, "POST", f"/api/posts/{pid}/likes").status_code == 200
        assert _mutate(b, "PUT", f"/api/posts/{pid}/saved").status_code == 200
        assert _mutate(b, "POST", f"/api/posts/{pid}/comments",
                       json={"content": "on victim post"}).status_code == 201
        # victim comments on the friend's post (author direction)
        assert _mutate(a, "POST", f"/api/posts/{b_pid}/comments",
                       json={"content": "on friend post"}).status_code == 201

        resp = _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True

        # User row physically gone.
        assert not _user_exists("t23d_victim", tmp_engine)

        with tmp_engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM posts WHERE id = :id"), {"id": pid}
            ).scalar() == 0
            # Friend's rows that referenced the victim are gone...
            assert conn.execute(text(
                "SELECT COUNT(*) FROM follows WHERE follower_id = :v "
                "OR followed_id = :v"), {"v": victim_id}
            ).scalar() == 0
            assert conn.execute(text(
                "SELECT COUNT(*) FROM saved_posts WHERE post_id = :pid"), {"pid": pid}
            ).scalar() == 0
            # ...but the friend's own content survives.
            assert conn.execute(text(
                "SELECT COUNT(*) FROM posts WHERE id = :id"), {"id": b_pid}
            ).scalar() == 1

        # Victim sessions revoked — the cookie is dead.
        assert a.get("/api/profile/me").status_code == 401

    def test_delete_forbids_signin_again(self, client, tmp_engine):
        a = TestClient(_app())
        _login(a, "t23d_gone")
        assert _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"}).status_code == 200
        r = TestClient(_app())
        resp = r.post("/api/auth/login",
                      json={"identifier": "t23d_gone", "password": "securepass1"})
        assert resp.status_code == 401

    def test_delete_removes_media_files(self, client, tmp_engine, isolated_uploads):
        a = TestClient(_app())
        _login(a, "t23d_files")
        pid = _post(a)
        resp = a.post(
            f"/api/posts/{pid}/media",
            files={"file": ("a.png", PNG_OK, "image/png")},
            headers=_csrf_headers(a),
        )
        assert resp.status_code == 201, resp.text
        stored = resp.json()["url"].rsplit("/", 1)[-1]
        assert (isolated_uploads / stored).is_file()

        assert _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"}).status_code == 200
        assert not (isolated_uploads / stored).exists(), \
            "post media file must be unlinked on account deletion"


# ---------------------------------------------------------------------------
# Tombstone — 30-day identifier reservation
# ---------------------------------------------------------------------------

class TestTombstone:
    def test_delete_writes_tombstone_and_blocks_register(
        self, client, tmp_engine,
    ):
        a = TestClient(_app())
        _login(a, "t23d_tomb")
        assert _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"}).status_code == 200

        row = _tombstone(tmp_engine, "t23d_tomb")
        assert row is not None
        assert row[1] == "t23d_tomb@t23d.com"

        r = TestClient(_app())
        # Same username (fresh email) -> 409
        resp = r.post("/api/auth/register", json={
            "username": "t23d_tomb", "email": "new@example.com",
            "password": "securepass1",
        })
        assert resp.status_code == 409, resp.text
        assert "30" in resp.json()["error"]["message"]
        # Same email (fresh username) -> 409
        resp = r.post("/api/auth/register", json={
            "username": "totally_fresh", "email": "t23d_tomb@t23d.com",
            "password": "securepass1",
        })
        assert resp.status_code == 409, resp.text

    def test_expired_tombstone_releases_identifiers(self, client, tmp_engine):
        a = TestClient(_app())
        _login(a, "t23d_old")
        assert _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"}).status_code == 200

        # Push the tombstone 31 days into the past (> 30-day window).
        _backdate_tombstone(tmp_engine, "t23d_old", days=31)

        r = TestClient(_app())
        resp = r.post("/api/auth/register", json={
            "username": "t23d_old", "email": "t23d_old@t23d.com",
            "password": "securepass1",
        })
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Moderation evidence survives deletion (anonymized)
# ---------------------------------------------------------------------------

class TestReportEvidence:
    def test_delete_anonymizes_but_keeps_reports(
        self, client, tmp_session, tmp_engine,
    ):
        a = TestClient(_app())
        _login(a, "t23d_rtarget")
        pid = _post(a, "reported post")
        victim_id = a.get("/api/users/t23d_rtarget").json()["id"]

        b = TestClient(_app())
        _login(b, "t23d_reporter")
        created = _mutate(b, "POST", "/api/reports", json={
            "target_user_id": victim_id, "reason": "spam", "post_id": pid,
        })
        assert created.status_code == 201, created.text
        report_id = created.json()["id"]

        # Delete the TARGET (post content also goes).
        assert _mutate(a, "POST", "/api/account/delete",
                       json={"password": "securepass1"}).status_code == 200

        with tmp_engine.connect() as conn:
            row = conn.execute(
                text("SELECT reporter_id, target_user_id, post_id, status "
                     "FROM reports WHERE id = :id"),
                {"id": report_id},
            ).one()
        # Evidence row survives, anonymized: target NULL, post pin NULL (the
        # post cascaded SET NULL), reporter still intact, status preserved.
        assert row[1] is None
        assert row[2] is None
        assert row[0] is not None
        assert row[3] == "pending"

        # Moderator queue still shows it (target_user null).
        mod = TestClient(_app())
        _make_moderator(mod, tmp_session, "t23d_mod")
        items = mod.get("/api/reports").json()
        shown = [r for r in items if r["id"] == report_id]
        assert len(shown) == 1
        assert shown[0]["target_user"] is None
        assert shown[0]["reporter"]["username"] == "t23d_reporter"

    def test_expired_reports_hidden_from_queue_and_404_on_resolve(
        self, client, tmp_session, tmp_engine,
    ):
        a = TestClient(_app())
        _login(a, "t23d_exp_target")
        pid = _post(a)
        victim_id = a.get("/api/users/t23d_exp_target").json()["id"]

        b = TestClient(_app())
        _login(b, "t23d_exp_reporter")
        old = _mutate(b, "POST", "/api/reports", json={
            "target_user_id": victim_id, "reason": "spam", "post_id": pid,
        })
        assert old.status_code == 201, old.text
        report_id = old.json()["id"]
        _backdate_report(tmp_engine, report_id, days=31)

        mod = TestClient(_app())
        _make_moderator(mod, tmp_session, "t23d_exp_mod")
        items = mod.get("/api/reports").json()
        assert all(r["id"] != report_id for r in items), \
            "report past the 30-day retention window must be hidden from the queue"
        # Resolving an expired row is a 404 (treated as purged).
        resp = _mutate(mod, "POST", f"/api/reports/{report_id}/resolve")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Web surface
# ---------------------------------------------------------------------------

class TestWebDelete:
    def test_web_delete_wrong_password_rerenders(self, client, tmp_engine):
        c = TestClient(_app(), follow_redirects=False)
        _login(c, "t23d_web")
        resp = _mutate(c, "POST", "/web/account/delete", data={"password": "nope-nope1"})
        assert resp.status_code == 200  # re-render with error
        assert "không đúng" in resp.text
        assert _user_exists("t23d_web", tmp_engine)

    def test_web_delete_success(self, client, tmp_engine):
        c = TestClient(_app(), follow_redirects=False)
        _login(c, "t23d_web2")
        pid = _post(c, "bye")
        assert _mutate(c, "POST", "/web/account/delete",
                       data={"password": "securepass1"}).status_code == 303
        assert not _user_exists("t23d_web2", tmp_engine)
        assert _tombstone(tmp_engine, "t23d_web2") is not None
        with tmp_engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM posts WHERE id = :id"),
                {"id": pid},
            ).scalar() == 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_uploads(tmp_path, monkeypatch):
    import ting_ting.media as media_mod

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(media_mod, "UPLOADS_DIR", uploads)
    return uploads
