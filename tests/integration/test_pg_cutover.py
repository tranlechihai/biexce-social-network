"""PostgreSQL cutover gate — runs ONLY when ``TING_PG_TEST_URL`` is set.

The CI ``postgres`` job provides the URL (job's postgres service). For local
verification against any PostgreSQL instance:

    TING_PG_TEST_URL=postgresql+psycopg://user:pass@localhost:5432/testdb \
        pytest -q tests/integration/test_pg_cutover.py

The URL's database is DESTROYED AND REBUILT by these tests (public schema is
dropped). Never point this at data you need.

What this file proves:
* migrations 0001->head apply cleanly on real PostgreSQL;
* the 0011 parity fixes are present (mute check constraint, hidden_posts
  gone, reports.status default, sequences on 0010-rebuilt tables);
* app startup fails closed on an unmanaged database and auto-migrates a
  fresh empty one;
* the one-shot data copy preserves rows/ids from a real SQLite database,
  and re-anchors sequences so the next insert cannot collide;
* the API works end-to-end on PostgreSQL (register/login/post/feed).
"""

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.skipif(
    not os.environ.get("TING_PG_TEST_URL"),
    reason="TING_PG_TEST_URL not set (no PostgreSQL target)",
)

PG_URL = os.environ.get("TING_PG_TEST_URL", "")


def _fresh_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def _upgrade_head(engine) -> None:
    from alembic import command as alembic_command

    from ting_ting.database import _alembic_config, _with_database_url
    with _with_database_url(engine):
        alembic_command.upgrade(_alembic_config(), "head")


def _public_tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).fetchall()
        }


@pytest.fixture(scope="module")
def pg_engine():
    """PG engine whose public schema carries the full migration chain at head."""
    engine = create_engine(PG_URL, pool_pre_ping=True)
    _fresh_schema(engine)
    _upgrade_head(engine)
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. Migration chain + parity fixes
# ---------------------------------------------------------------------------

def test_migrations_apply_and_head_stamped(pg_engine):
    from ting_ting.database import _alembic_head_revision, _current_revision
    from ting_ting.models import Base

    assert _current_revision(pg_engine) == _alembic_head_revision()
    assert set(Base.metadata.tables) <= _public_tables(pg_engine)


def test_p011_parity_fixes_present(pg_engine):
    with pg_engine.connect() as conn:
        constraints = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'mutes'::regclass"
                )
            ).fetchall()
        }
        # 0005's PG branch skipped this; 0011 must add it.
        assert "ck_mute_exactly_one_target" in constraints

        # Legacy table the PG branch of 0005 never dropped.
        assert "hidden_posts" not in _public_tables(pg_engine)

        # 0010's rebuild dropped the default; 0011 restores it.
        default = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'reports' AND column_name = 'status'"
            )
        ).scalar()
        assert default is not None and "'pending'" in default

        # 0010 rebuilt these tables via CREATE TABLE: their integer PKs must
        # carry sequences, otherwise inserts without an explicit id can never
        # work on PostgreSQL.
        for table in ("reports", "deleted_accounts"):
            sequence = conn.execute(
                text(f"SELECT pg_get_serial_sequence('{table}', 'id')")
            ).scalar()
            assert sequence, f"{table}.id has no backing sequence"


def test_startup_refuses_unmanaged_database(pg_engine):
    """Tables without an alembic_version stamp -> fail closed (no mutation)."""
    from ting_ting.database import _alembic_head_revision, validate_and_initialize_schema

    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("CREATE TABLE rogue_check (id INTEGER)"))
        conn.execute(text("INSERT INTO rogue_check VALUES (1)"))

    with pytest.raises(ValueError, match="no alembic_version"):
        validate_and_initialize_schema(pg_engine)

    # Restore: drop the rogue row/table, restamp at head.
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE rogue_check"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR NOT NULL PRIMARY KEY)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:h)"),
            {"h": _alembic_head_revision()},
        )


def test_fresh_database_is_migrated_at_startup(pg_engine):
    """An empty PostgreSQL database is migrated by the app start path itself
    (embedded alembic), mirroring the SQLite 'fresh file' behavior."""
    from ting_ting.database import _alembic_head_revision, _current_revision, validate_and_initialize_schema

    _fresh_schema(pg_engine)
    validate_and_initialize_schema(pg_engine)
    assert _current_revision(pg_engine) == _alembic_head_revision()


# ---------------------------------------------------------------------------
# 2. Data copy roundtrip (real SQLite -> PostgreSQL)
# ---------------------------------------------------------------------------

def test_copy_roundtrip_and_sequence_reanchor(pg_engine, tmp_path):
    from ting_ting.auth import hash_password
    from ting_ting.database import (
        _alembic_head_revision,
        _create_test_engine,
        _init_test_engine,
    )
    from ting_ting.migrate_data import copy_database
    from ting_ting.models import (
        Comment, Like, Post, Report, User, UserProfile,
    )

    # --- source: SQLite, stamped at head, populated -----------------------
    src_engine = _create_test_engine(str(tmp_path / "source.db"))
    _init_test_engine(src_engine)
    with src_engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR PRIMARY KEY)")
        )
        conn.execute(
            text("INSERT INTO alembic_version VALUES (:h)"),
            {"h": _alembic_head_revision()},
        )

    src = sessionmaker(bind=src_engine, expire_on_commit=False)
    with src() as s:
        alice = User(
            username="alice", email="alice@example.com",
            password_hash=hash_password("password123"),
        )
        bob = User(
            username="bob", email="bob@example.com",
            password_hash=hash_password("password456"),
        )
        s.add_all([alice, bob])
        s.flush()
        s.add(UserProfile(user_id=alice.id, location="Hanoi"))
        # Explicit large id: proves sequence re-anchoring past copied IDs.
        s.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash) "
                "VALUES (9001, 'gap', 'gap@example.com', :ph)"
            ),
            {"ph": hash_password("whatever1")},
        )
        post = Post(author_id=alice.id, content="hello pg", audience="PUBLIC")
        s.add(post)
        s.flush()
        parent = Comment(post_id=post.id, author_id=bob.id, content="parent")
        s.add(parent)
        s.flush()
        s.add(
            Comment(
                post_id=post.id, author_id=alice.id,
                content="child", parent_comment_id=parent.id,
            )
        )
        s.add(Like(user_id=bob.id, post_id=post.id))
        s.add(
            Report(
                reporter_id=bob.id, target_user_id=alice.id,
                post_id=post.id, reason="other", status="pending",
            )
        )
        s.commit()

    # --- target: fresh PostgreSQL at head, then copy -----------------------
    _fresh_schema(pg_engine)
    _upgrade_head(pg_engine)
    copied = copy_database(src_engine, pg_engine)
    assert copied["users"] == 3
    assert copied["posts"] == 1
    assert copied["comments"] == 2
    assert copied["likes"] == 1
    assert copied["reports"] == 1
    assert copied["deleted_accounts"] == 0

    # Values + primary keys preserved.
    with pg_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT u.id, u.username, p.content, c.content, c.parent_comment_id "
                "FROM users u "
                "JOIN posts p ON p.author_id = u.id "
                "JOIN comments c ON c.post_id = p.id "
                "WHERE u.username = 'alice'"
            )
        ).one()
    assert row[1] == "alice" and row[2] == "hello pg"
    assert row[3] == "child" and row[4] is not None

    # Sequence re-anchored: the next implicit id must clear the copied
    # maximum (9001), otherwise the first real insert after cutover collides.
    pg = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with pg() as s:
        s.add(User(username="after", email="after@example.com",
                   password_hash=hash_password("x12345678")))
        s.commit()
        new_id = s.execute(select(User.id).where(User.username == "after")).scalar_one()
    assert new_id > 9001

    src_engine.dispose()


def test_copy_refuses_drifted_source(pg_engine, tmp_path):
    """A source missing a current column is refused before anything is
    written to the target (row counts unchanged)."""
    from ting_ting.database import _create_test_engine, _init_test_engine
    from ting_ting.migrate_data import copy_database

    src_engine = _create_test_engine(str(tmp_path / "drifted.db"))
    _init_test_engine(src_engine)
    with src_engine.begin() as conn:
        conn.execute(text("ALTER TABLE users RENAME COLUMN bio TO biography"))

    def _target_users_count() -> int:
        if "users" not in _public_tables(pg_engine):
            return -1
        with pg_engine.connect() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM users")).scalar()

    before = _target_users_count()

    with pytest.raises(ValueError, match="missing columns"):
        copy_database(src_engine, pg_engine)

    assert _target_users_count() == before, "target was mutated before refusal"
    src_engine.dispose()


# ---------------------------------------------------------------------------
# 3. API smoke on PostgreSQL
# ---------------------------------------------------------------------------

def test_api_smoke_on_postgres(pg_engine):
    """Register/login/post/feed through the real stack, PG-backed."""
    from ting_ting.database import validate_and_initialize_schema
    from ting_ting.main import app
    from tests.conftest import CsrfTestClient

    import ting_ting.database as db_mod

    # Start from a known-clean database (also re-proves the startup path).
    _fresh_schema(pg_engine)
    validate_and_initialize_schema(pg_engine)

    orig_engine = db_mod._engine
    orig_session = db_mod._SessionLocal
    db_mod._engine = pg_engine
    db_mod._SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)
    try:
        client = CsrfTestClient(app)

        reg = client.post(
            "/api/v1/auth/register",
            json={
                "username": "pguser",
                "email": "pguser@example.com",
                "password": "password123",
            },
            follow_redirects=False,
        )
        assert reg.status_code in {200, 201}, reg.text

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pguser", "password": "password123"},
            follow_redirects=False,
        )
        assert login.status_code == 200, login.text

        created = client.post(
            "/api/v1/posts",
            json={"content": "posted from postgres", "audience": "PUBLIC"},
            follow_redirects=False,
        )
        assert created.status_code in {200, 201}, created.text
        post_id = created.json()["id"]

        # /feed returns list[PostResponse]
        feed = client.get("/api/v1/feed", follow_redirects=False)
        assert feed.status_code == 200, feed.text
        assert any(item.get("id") == post_id for item in feed.json())

        ready = client.get("/ready")
        assert ready.status_code == 200 and ready.json()["database"] == "ok"
    finally:
        db_mod._engine = orig_engine
        db_mod._SessionLocal = orig_session
