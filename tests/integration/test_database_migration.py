"""Migration baseline and fail-closed data-copy tests."""

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select

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
