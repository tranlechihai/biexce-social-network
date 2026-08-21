"""Integration tests for T-022 — SQLite runtime hardening.

Covers:
* WAL + busy_timeout applied to every runtime SQLite connection;
* moderator post delete removes the media files (no orphans);
* keyset pagination on /saved, /posts/{id}/comments, followers/following
  (stable pages, no dup/loss, visibility skips do not shrink pages);
* concurrent duplicate writes (friend request / block / mute) converge to
  client-safe responses (409 / 200) instead of 500.
"""

import threading

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from ting_ting.models import User


PNG_OK = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x00" * 400  # valid 1.2 KB PNG


def _app():
    from ting_ting.main import app

    return app


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_uploads(tmp_path, monkeypatch):
    import ting_ting.media as media_mod

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(media_mod, "UPLOADS_DIR", uploads)
    return uploads


def _csrf_headers(c: TestClient) -> dict:
    tok = c.cookies.get("ting_ting_csrf")
    return {"X-CSRF-Token": tok} if tok else {}


def _mutate(c: TestClient, method: str, url: str, **kw):
    headers = dict(kw.pop("headers", None) or {})
    headers.update(_csrf_headers(c))
    if headers:
        kw["headers"] = headers
    return c.request(method, url, **kw)


def _login(client: TestClient, username: str, password: str = "securepass1") -> None:
    client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@t22.com", "password": password,
    })
    resp = client.post(
        "/api/auth/login", json={"identifier": username, "password": password},
    )
    assert resp.status_code == 200, resp.text


def _make_moderator(client: TestClient, tmp_session, username: str) -> None:
    client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@t22.com", "password": "securepass1",
    })
    row = tmp_session.query(User).filter(User.username == username).first()
    row.is_moderator = True
    tmp_session.commit()
    _login(client, username)


def _create_post(client: TestClient, content: str = "hello") -> int:
    resp = client.post("/api/posts", json={"content": content, "audience": "PUBLIC"})
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()["id"]


def _device() -> TestClient:
    """Second browser on the same (already swapped) test engine."""
    return TestClient(_app())


# ---------------------------------------------------------------------------
# WAL + busy_timeout
# ---------------------------------------------------------------------------

class TestSqlitePragmas:
    def test_app_engine_uses_wal_and_busy_timeout(self, client, tmp_engine):
        import sqlalchemy as sa

        with tmp_engine.connect() as conn:
            journal = conn.execute(sa.text("PRAGMA journal_mode")).scalar()
            timeout = conn.execute(sa.text("PRAGMA busy_timeout")).scalar()
            fk = conn.execute(sa.text("PRAGMA foreign_keys")).scalar()
        assert journal == "wal"
        assert int(timeout) == 5000
        assert int(fk) == 1

    def test_memory_db_does_not_break_wal_setting(self, client):
        import sqlalchemy as sa

        import ting_ting.database as db_mod

        engine = db_mod.create_engine("sqlite://")
        db_mod.enable_sqlite_runtime_pragmas(engine)
        try:
            with engine.connect() as conn:
                journal = conn.execute(sa.text("PRAGMA journal_mode")).scalar()
        finally:
            engine.dispose()
        assert journal == "memory"  # in-memory DB keeps memory journal


# ---------------------------------------------------------------------------
# Moderator delete cleans media files
# ---------------------------------------------------------------------------

class TestModDeleteMediaCleanup:
    def test_mod_delete_post_removes_media_files(self, client, tmp_session, isolated_uploads):
        _login(client, "t22_author")
        post_id = _create_post(client)
        resp = client.post(
            f"/api/posts/{post_id}/media",
            files={"file": ("a.png", PNG_OK, "image/png")},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        stored = resp.json()["url"].rsplit("/", 1)[-1]
        assert (isolated_uploads / stored).is_file()

        client2 = _device()
        _make_moderator(client2, tmp_session, "t22_mod")

        resp = _mutate(client2, "DELETE", f"/api/mod/posts/{post_id}")
        assert resp.status_code == 200, resp.text

        assert not (isolated_uploads / stored).exists(), \
            "media file orphaned after mod delete"

    def test_mod_delete_post_without_media_still_works(self, client, tmp_session):
        _login(client, "t22_author2")
        post_id = _create_post(client)

        client2 = _device()
        _make_moderator(client2, tmp_session, "t22_mod2")
        resp = _mutate(client2, "DELETE", f"/api/mod/posts/{post_id}")
        assert resp.status_code == 200, resp.text

    def test_author_delete_still_removes_media(self, client, isolated_uploads):
        """Regression guard for the author path (same convention)."""
        _login(client, "t22_author3")
        post_id = _create_post(client)
        resp = client.post(
            f"/api/posts/{post_id}/media",
            files={"file": ("b.png", PNG_OK, "image/png")},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        stored = resp.json()["url"].rsplit("/", 1)[-1]

        resp = _mutate(client, "DELETE", f"/api/posts/{post_id}")
        assert resp.status_code == 200, resp.text
        assert not (isolated_uploads / stored).exists()


# ---------------------------------------------------------------------------
# Comments keyset pagination
# ---------------------------------------------------------------------------

class TestCommentsPagination:
    def _setup(self, client):
        _login(client, "t22_cmts")
        post_id = _create_post(client)
        for i in range(25):
            resp = client.post(
                f"/api/posts/{post_id}/comments", json={"content": f"comment-{i:02d}"},
            )
            assert resp.status_code == status.HTTP_201_CREATED, resp.text
        return post_id

    def test_cursor_pages_cover_all_without_dup(self, client):
        post_id = self._setup(client)
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            url = f"/api/posts/{post_id}/comments?limit=20"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert 0 < len(body) <= 20
            seen.extend(c["content"] for c in body)
            cursor = resp.headers.get("X-Next-Cursor")
            pages += 1
            assert pages <= 3
            if not cursor:
                break

        assert seen == [f"comment-{i:02d}" for i in range(25)]  # oldest first, no dup

    def test_offset_legacy_still_works(self, client):
        post_id = self._setup(client)
        resp = client.get(f"/api/posts/{post_id}/comments?limit=10&offset=20")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [c["content"] for c in body] == [f"comment-{i:02d}" for i in range(20, 25)]
        assert resp.headers.get("X-Next-Cursor") is None

        # limit smaller than the remaining tail: cursor points past the page.
        resp = client.get(f"/api/posts/{post_id}/comments?limit=2&offset=20")
        assert [c["content"] for c in resp.json()] == ["comment-20", "comment-21"]


# ---------------------------------------------------------------------------
# Saved keyset pagination
# ---------------------------------------------------------------------------

class TestSavedPagination:
    def _setup(self, client, n: int):
        _login(client, "t22_saved")
        post_ids: list[int] = []
        for i in range(n):
            post_ids.append(_create_post(client, f"post {i}"))
        for pid in post_ids:
            resp = client.request("PUT", f"/api/posts/{pid}/saved")
            assert resp.status_code == 200, resp.text
        return post_ids

    def test_cursor_pages_newest_saved_first_without_dup(self, client):
        post_ids = self._setup(client, 12)
        seen_ids: list[int] = []
        cursor = None
        pages = 0
        while True:
            url = "/api/saved?limit=5"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert 0 < len(body) <= 5
            seen_ids.extend(p["id"] for p in body)
            cursor = resp.headers.get("X-Next-Cursor")
            pages += 1
            assert pages <= 4
            if not cursor:
                break

        # Saved in creation order (ascending ids) -> newest-first = reversed.
        assert seen_ids == list(reversed(post_ids))

    def test_deleted_post_is_skipped_and_page_still_full(self, client):
        post_ids = self._setup(client, 6)
        del_resp = _mutate(client, "DELETE", f"/api/posts/{post_ids[-1]}")
        assert del_resp.status_code in (200, 204)

        resp = client.get("/api/saved?limit=5")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 6 saved - 1 deleted = 5 visible -> one full page, no next cursor.
        assert len(body) == 5
        assert post_ids[-1] not in [p["id"] for p in body]
        assert not resp.headers.get("X-Next-Cursor")


# ---------------------------------------------------------------------------
# Followers/following keyset pagination
# ---------------------------------------------------------------------------

def _make_target_and_followers(target_name: str, n: int = 5):
    target = _device()
    _login(target, target_name)
    for i in range(n):
        fan = _device()
        _login(fan, f"{target_name}_fan_{i}")
        uid = fan.get(f"/api/users/{target_name}").json()["id"]
        resp = _mutate(fan, "PUT", f"/api/social/follows/{uid}")
        assert resp.status_code == 200, resp.text
    return target


class TestFollowersPagination:
    def test_followers_paged(self, client, tmp_session, tmp_engine):
        _make_target_and_followers("t22_pt", n=5)
        viewer = _device()
        _login(viewer, "t22_viewer_a")

        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            url = "/api/users/t22_pt/followers?limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            resp = viewer.get(url)
            assert resp.status_code == 200, resp.text
            page = [u["username"] for u in resp.json()]
            assert 0 < len(page) <= 2
            seen.extend(page)
            cursor = resp.headers.get("X-Next-Cursor")
            pages += 1
            assert pages <= 4
            if not cursor:
                break

        assert set(seen) == {f"t22_pt_fan_{i}" for i in range(5)}
        assert len(seen) == len(set(seen))

    def test_followers_without_limit_returns_full_graph(self, client, tmp_session, tmp_engine):
        _make_target_and_followers("t22_pt2", n=5)
        viewer = _device()
        _login(viewer, "t22_viewer_b")
        resp = viewer.get("/api/users/t22_pt2/followers")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 5
        assert not resp.headers.get("X-Next-Cursor")


# ---------------------------------------------------------------------------
# Concurrency dedup (race -> safe 4xx/200, never 500)
# ---------------------------------------------------------------------------

def _run_parallel(funs: list) -> list:
    results: list = [None] * len(funs)
    barrier = threading.Barrier(len(funs))

    def worker(i, fn):
        barrier.wait()
        results[i] = fn()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(funs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


class TestConcurrencyDedup:
    def _pair(self):
        c1, c2 = _device(), _device()
        _login(c1, "t22_race_a")
        _login(c2, "t22_race_b")
        uid_a = c1.get("/api/users/t22_race_a").json()["id"]
        uid_b = c2.get("/api/users/t22_race_b").json()["id"]
        return c1, c2, uid_a, uid_b

    def test_concurrent_friend_requests_never_500(self, client, tmp_session, tmp_engine):
        c1, c2, uid_a, uid_b = self._pair()
        results = _run_parallel([
            lambda: _mutate(c1, "POST", "/api/social/requests",
                            json={"target_user_id": uid_b}).status_code,
            lambda: _mutate(c2, "POST", "/api/social/requests",
                            json={"target_user_id": uid_a}).status_code,
        ])
        # Each direction is a distinct pair: both succeed OR one side hits
        # its own re-send; what matters: never a 500.
        assert all(code in (201, 409) for code in results), results

    def test_concurrent_same_pair_request_never_500(self, client, tmp_session, tmp_engine):
        # True same-canonical-pair race: each client requests the OTHER —
        # both writes hit the same unique (canonical_left, canonical_right).
        c1, c2, uid_a, uid_b = self._pair()
        results = _run_parallel([
            lambda: _mutate(c1, "POST", "/api/social/requests",
                            json={"target_user_id": uid_b}).status_code,
            lambda: _mutate(c2, "POST", "/api/social/requests",
                            json={"target_user_id": uid_a}).status_code,
        ])
        assert sorted(results) == [201, 409], results
        assert 500 not in results

    def test_concurrent_blocks_same_pair_never_500(self, client, tmp_session, tmp_engine):
        # Same (blocker, blocked) pair raced from two sessions of one account.
        c1, c2 = _device(), _device()
        _login(c1, "t22_cb_a")
        _login(c2, "t22_cb_a")  # second session, same account
        c3 = _device()
        _login(c3, "t22_cb_target")
        uid_t = c3.get("/api/users/t22_cb_target").json()["id"]

        results = _run_parallel([
            lambda: _mutate(c1, "POST", "/api/social/blocks",
                            json={"target_user_id": uid_t}).status_code,
            lambda: _mutate(c2, "POST", "/api/social/blocks",
                            json={"target_user_id": uid_t}).status_code,
        ])
        # One wins (201); the loser gets 409 (lost the race) or 201 (sequential).
        assert all(code in (201, 409) for code in results), results

    def test_concurrent_mutes_same_muter_converge_200(self, client, tmp_session, tmp_engine):
        # Same (muter, target) raced from two sessions; PUT is idempotent,
        # the race converges to the same end state -> 200 both sides.
        c1, c2 = _device(), _device()
        _login(c1, "t22_cm_a")
        _login(c2, "t22_cm_a")
        c3 = _device()
        _login(c3, "t22_cm_target")
        uid_t = c3.get("/api/users/t22_cm_target").json()["id"]

        results = _run_parallel([
            lambda: _mutate(c1, "PUT", f"/api/social/mutes/{uid_t}").status_code,
            lambda: _mutate(c2, "PUT", f"/api/social/mutes/{uid_t}").status_code,
        ])
        assert results == [200, 200], results
