"""Migration baseline and fail-closed data-copy tests."""

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text

from ting_ting.migrate_data import copy_database
from ting_ting.models import (
    Activity,
    Base,
    Comment,
    Like,
    Post,
    PostMedia,
    RefreshToken,
    Repost,
    SavedPost,
    User,
)


def _run(url: str, action: str, target: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    previous = os.environ.pop("TING_DATABASE_URL", None)
    try:
        if action == "upgrade":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
    finally:
        if previous is not None:
            os.environ["TING_DATABASE_URL"] = previous


def _upgrade(url: str) -> None:
    _run(url, "upgrade", "head")


def test_alembic_baseline_creates_full_schema(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'alembic.db'}"

    _upgrade(url)

    engine = create_engine(url)
    assert set(Base.metadata.tables).issubset(set(inspect(engine).get_table_names()))
    engine.dispose()


def test_copy_preserves_ids_and_counts(tmp_path: Path):
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    target = create_engine(target_url)
    Base.metadata.create_all(source)
    _upgrade(target_url)

    with source.begin() as connection:
        connection.execute(User.__table__.insert(), [{
            "id": 7,
            "username": "migrated",
            "email": "migrated@example.com",
            "password_hash": "hash",
        }])
        connection.execute(Post.__table__.insert(), [{
            "id": 11,
            "author_id": 7,
            "content": "preserved",
            "audience": "ONLY_ME",
        }])

    copied = copy_database(source, target, require_postgresql=False)

    with target.connect() as connection:
        user = connection.execute(select(User)).one()
        post = connection.execute(select(Post)).one()
    assert copied["users"] == 1
    assert copied["posts"] == 1
    assert user.id == 7
    assert post.id == 11
    source.dispose()
    target.dispose()


def test_copy_refuses_fk_orphan_source_without_mutation(tmp_path: Path):
    """A source with orphan rows (e.g. left by maintenance done on a
    foreign_keys=OFF connection — SQLite's default) is refused BEFORE the
    copy, with a message that names the offending rows, and the target is
    left untouched."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    Base.metadata.create_all(source)
    _upgrade(f"sqlite:///{tmp_path / 'target.db'}")

    # Default sqlite3 connections run with foreign_keys=OFF, so this orphan
    # insert succeeds — exactly how dev/maintenance rows end up dangling.
    with source.connect() as connection:
        connection.execute(RefreshToken.__table__.insert(), [{
            "id": 3,
            "session_id": "deadbeef" * 4,
            "user_id": 999,
            "token_hash": "x" * 64,
            "created_at": now,
            "expires_at": now,
        }])
        connection.commit()

    with pytest.raises(ValueError, match="foreign-key violation"):
        copy_database(source, target, require_postgresql=False)

    # Refusal happened before any write: target still pristine.
    with target.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 0
    source.dispose()
    target.dispose()


def test_copy_refuses_nonempty_target_without_mutation(tmp_path: Path):
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    Base.metadata.create_all(source)
    Base.metadata.create_all(target)
    with target.begin() as connection:
        connection.execute(User.__table__.insert(), [{
            "username": "existing",
            "email": "existing@example.com",
            "password_hash": "hash",
        }])

    with pytest.raises(ValueError, match="not empty"):
        copy_database(source, target, require_postgresql=False)

    with target.connect() as connection:
        assert connection.scalar(select(User.username)) == "existing"
    source.dispose()
    target.dispose()


def _seed_post_graph(url: str) -> None:
    """Two users, two posts and one child row of every posts-dependent kind."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(User.__table__.insert(), [
                {"id": 1, "username": "alice", "email": "a@example.com", "password_hash": "h"},
                {"id": 2, "username": "bob", "email": "b@example.com", "password_hash": "h"},
            ])
            connection.execute(Post.__table__.insert(), [
                {"id": 1, "author_id": 1, "content": "hello world", "audience": "PUBLIC",
                 "created_at": now, "updated_at": now},
                {"id": 2, "author_id": 2, "content": "private note", "audience": "ONLY_ME",
                 "created_at": now, "updated_at": now},
            ])
            # Uniform keys across rows — SQLAlchemy compiles the multi-row INSERT from
            # the first dict, so a key only present later would be dropped.
            connection.execute(Comment.__table__.insert(), [
                {"id": 1, "post_id": 1, "author_id": 2, "parent_comment_id": None,
                 "content": "first reply", "created_at": now},
                {"id": 2, "post_id": 1, "author_id": 1, "parent_comment_id": 1,
                 "content": "nested", "created_at": now},
            ])
            connection.execute(Like.__table__.insert(),
                               [{"id": 1, "user_id": 2, "post_id": 1, "created_at": now}])
            connection.execute(Repost.__table__.insert(),
                               [{"id": 1, "user_id": 2, "post_id": 1, "created_at": now}])
            connection.execute(SavedPost.__table__.insert(),
                               [{"id": 1, "user_id": 1, "post_id": 2, "created_at": now}])
            connection.execute(Activity.__table__.insert(), [
                {"id": 1, "user_id": 1, "actor_id": 2, "kind": "like", "post_id": 1,
                 "created_at": now},
            ])
            connection.execute(PostMedia.__table__.insert(), [
                {"id": 1, "post_id": 1, "path": "uploads/x.png", "media_type": "image"},
            ])
    finally:
        engine.dispose()


def _child_counts(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        return {
            t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in _CHILD_TABLES
        } | {
            "posts": db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "fkc": db.execute("PRAGMA foreign_key_check").fetchall(),
        }
    finally:
        db.close()


_CHILD_TABLES = ("comments", "likes", "reposts", "saved_posts", "activities", "post_media")
_EXPECTED = {"posts": 2, **{t: 1 for t in _CHILD_TABLES},
             "comments": 2}


def _post_fk_state(db_path: Path) -> tuple:
    db = sqlite3.connect(db_path)
    try:
        fks = db.execute("PRAGMA foreign_key_list(posts)").fetchall()
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
        ).fetchone()[0]
        indexes = {r[1] for r in db.execute("PRAGMA index_list(posts)")}
        return fks, sql, indexes
    finally:
        db.close()


def test_0007_roundtrip_preserves_post_children(tmp_path: Path):
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_path}"
    _run(url, "upgrade", "20260819_0006")
    _seed_post_graph(url)
    _run(url, "upgrade", "head")

    state = _child_counts(db_path)
    assert state["fkc"] == []
    for t in ("posts", *_CHILD_TABLES):
        assert state[t] == _EXPECTED[t], (t, state[t], state)
    fks, sql, indexes = _post_fk_state(db_path)
    assert any(r[0] == 0 and r[6] == "CASCADE" for r in fks), fks
    assert "ck_post_audience" in sql
    assert {"ix_posts_author_id", "ix_posts_created_at_id"} <= indexes

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT content FROM posts WHERE id = 1").fetchone()[0] == "hello world"
        assert db.execute("SELECT parent_comment_id FROM comments WHERE id = 2").fetchone()[0] == 1
    finally:
        db.close()

    _run(url, "downgrade", "20260819_0006")
    after = _child_counts(db_path)
    assert after["fkc"] == []
    for t in ("posts", *_CHILD_TABLES):
        assert after[t] == _EXPECTED[t], (t, after[t], after)
    fks, sql, indexes = _post_fk_state(db_path)
    assert not any(r[6] == "CASCADE" for r in fks), fks
    assert "ck_post_audience" in sql
    assert {"ix_posts_author_id", "ix_posts_created_at_id"} <= indexes


def test_0007_upgrade_fails_closed_on_orphan_posts(tmp_path: Path):
    db_path = tmp_path / "orphan.db"
    url = f"sqlite:///{db_path}"
    _run(url, "upgrade", "20260819_0006")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(User.__table__.insert(), [{
                "id": 1, "username": "alice", "email": "a@example.com", "password_hash": "h",
            }])
            connection.execute(Post.__table__.insert(), [{
                "id": 5, "author_id": 999, "content": "orphan", "audience": "PUBLIC",
                "created_at": now, "updated_at": now,
            }])
    finally:
        engine.dispose()

    with pytest.raises(ValueError, match="missing user"):
        _run(url, "upgrade", "head")

    # Transaction rolled back: revision unchanged, posts row and legacy FK intact.
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20260819_0006"
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        fks = db.execute("PRAGMA foreign_key_list(posts)").fetchall()
        assert fks and all(r[6] != "CASCADE" for r in fks)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 0012 — NULL-safe report dedup (reports rebuild on SQLite)
# ---------------------------------------------------------------------------

def _seed_reports_at_0011(url: str, db_path: Path) -> tuple[int, int, int]:
    """Upgrade to 0011 (legacy ``uq_report_target`` shape) and seed:
    3 reports (bare, anchored-post, anchored-comment) + 1 duplicate of the
    anchored-post report that the legacy 4-column constraint LEGALLY admits
    (NULL comment_id), proving the gap 0012 closes.
    """
    _run(url, "upgrade", "20260821_0011")
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    now_sql = now.isoformat(sep=" ")
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(User.__table__.insert(), [
                {"id": 1, "username": "rep_a", "email": "ra@x.com",
                 "password_hash": "h"},
                {"id": 2, "username": "rep_b", "email": "rb@x.com",
                 "password_hash": "h"},
                {"id": 3, "username": "rep_c", "email": "rc@x.com",
                 "password_hash": "h"},
            ])
            c.execute(Post.__table__.insert(), [{
                "id": 7, "author_id": 2, "content": "seed post",
                "audience": "PUBLIC", "created_at": now, "updated_at": now,
            }])
            c.execute(Comment.__table__.insert(), [{
                "id": 9, "post_id": 7, "author_id": 3,
                "content": "seed comment", "created_at": now,
            }])
            c.execute(
                text(
                    f"""
                    INSERT INTO reports (id, reporter_id, target_user_id,
                                         post_id, comment_id, reason,
                                         status, created_at)
                    VALUES
                      (1, 1, 2, NULL, NULL, 'spam', 'pending', '{now_sql}'),
                      (2, 1, 2, 7, NULL, 'harassment', 'pending', '{now_sql}'),
                      (3, 1, 3, 7, 9, 'other', 'pending', '{now_sql}')
                    """
                )
            )
            # The legacy constraint admits this duplicate (comment_id NULL).
            c.execute(
                text(
                    f"""
                    INSERT INTO reports (id, reporter_id, target_user_id,
                                         post_id, comment_id, reason,
                                         status, created_at)
                    VALUES (4, 1, 2, 7, NULL, 'spam', 'pending', '{now_sql}')
                    """
                )
            )
    finally:
        engine.dispose()
    return 7, 2, 1  # post_id, target_id, reporter_id


def test_0012_sqlite_rebuild_and_dedup_roundtrip(tmp_path: Path):
    db_path = tmp_path / "dedup.db"
    url = f"sqlite:///{db_path}"
    post_id, target_id, reporter_id = _seed_reports_at_0011(url, db_path)

    # The seeded legacy duplicate (row 4) blocks the index: the upgrade must
    # fail CLOSED with a clear error and leave the database untouched.
    with pytest.raises(ValueError, match="ux_reports_dedup"):
        _upgrade(url)
    con = sqlite3.connect(db_path)
    try:
        assert con.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "20260821_0011"
        # Failure left everything in place (transactional DDL rolled back).
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 4
    finally:
        con.close()

    # Resolve the duplicate, then upgrade to head (0012): rebuild reports.
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM reports WHERE id = 4")
        con.commit()
    finally:
        con.close()
    _upgrade(url)

    con = sqlite3.connect(db_path)
    try:
        # The 3 surviving rows made it through the rebuild.
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 3
        # Inline unique constraint gone from the table DDL...
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reports'"
        ).fetchone()[0]
        assert "uq_report_target" not in ddl
        # ...and the functional index is present.
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reports'"
        ).fetchall()}
        assert "ux_reports_dedup" in names, names
    finally:
        con.close()

    # The functional index rejects the duplicate the legacy constraint allowed.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            f"""
            INSERT INTO reports (reporter_id, target_user_id, post_id,
                                 comment_id, reason, status, created_at)
            VALUES ({reporter_id}, {target_id}, {post_id}, NULL, 'spam',
                    'pending', '{datetime.now(timezone.utc).isoformat(sep=' ')}')
            """
        )
        con.commit()
        raise AssertionError("duplicate post report was accepted after 0012")
    except sqlite3.IntegrityError:
        pass
    finally:
        con.close()

    # Wedge guard: deleting the post SET-NULLs the anchored report's post_id
    # (and the comment's deletion SET-NULLs comment_id) — that must NOT
    # collide with the bare report of the same (reporter, target).
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        con.commit()
        rows = con.execute(
            "SELECT post_id, comment_id FROM reports WHERE reporter_id = 1"
            " ORDER BY id"
        ).fetchall()
        assert rows, "reports missing after post delete"
        assert all(r[0] is None and r[1] is None for r in rows), rows
    finally:
        con.close()

    # Downgrade restores the legacy UNIQUE and keeps every row.
    _run(url, "downgrade", "20260821_0011")
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 3
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reports'"
        ).fetchone()[0]
        assert "uq_report_target" in ddl
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reports'"
        ).fetchall()}
        assert "ux_reports_dedup" not in names, names
    finally:
        con.close()

    # ...and re-upgrade returns to the 0012 shape.
    _upgrade(url)
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 3
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reports'"
        ).fetchall()}
        assert "ux_reports_dedup" in names, names
    finally:
        con.close()
