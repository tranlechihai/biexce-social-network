"""Integration tests for schema initialization — AC9.

Verify:
* Creating required structures on a new target
* Leaving a compatible initialized target unchanged
* Failing before mutation on incompatible schema
* No drop, truncate, or database replacement
"""

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from ting_ting.database import (
    _create_test_engine,
    _init_test_engine,
    validate_and_initialize_schema,
)
from ting_ting.models import User


# ---------------------------------------------------------------------------
# AC9: Schema initialization
# ---------------------------------------------------------------------------

class TestAC9SchemaInit:
    """Tests run against file-based SQLite to inspect schema changes."""

    def test_creates_on_fresh_target(self, tmp_path: Path):
        """A brand-new database gets all required tables created."""
        db_path = str(tmp_path / "fresh.db")
        engine = _create_test_engine(db_path)

        validate_and_initialize_schema(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "users" in tables

        cols = {c["name"] for c in inspector.get_columns("users")}
        expected = {"id", "username", "email", "password_hash", "display_name", "bio"}
        assert expected.issubset(cols)
        engine.dispose()

    def test_leaves_compatible_initialized_target_unchanged(self, tmp_path: Path):
        """An already-compatible database is not mutated or truncated."""
        db_path = str(tmp_path / "existing.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)

        # Insert a record before re-initialization
        from sqlalchemy.orm import sessionmaker
        sm = sessionmaker(bind=engine, expire_on_commit=False)
        with sm() as s:
            u = User(
                username="preserved",
                email="preserved@example.com",
                password_hash="$2b$12$fakehashfordata",
            )
            s.add(u)
            s.commit()

        # Re-run initialization (simulating restart)
        validate_and_initialize_schema(engine)

        # Existing record must survive
        with sm() as s:
            row = s.get(User, 1)
        assert row is not None
        assert row.username == "preserved"
        engine.dispose()

    def test_incompatible_schema_fails_before_mutation(self, tmp_path: Path):
        """Missing required columns → ValueError before any mutation."""
        db_path = str(tmp_path / "bad_schema.db")
        engine = _create_test_engine(db_path)

        # Create a 'users' table missing required columns
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.commit()

        with pytest.raises(ValueError, match="Incompatible"):
            validate_and_initialize_schema(engine)

        # Verify the bad table was NOT dropped or altered
        inspector = inspect(engine)
        assert "users" in set(inspector.get_table_names())
        cols = {c["name"] for c in inspector.get_columns("users")}
        assert cols == {"id", "name"}
        engine.dispose()

    def test_unsupported_backend_rejected(self):
        """A backend other than SQLite/PostgreSQL is rejected immediately."""
        # Avoid loading a real non-SQLite driver; create a minimal stub.
        class _MockEngine:
            name = "mysql"
        with pytest.raises(ValueError, match="sqlite or postgresql"):
            validate_and_initialize_schema(_MockEngine())

    def test_no_implicit_drop_or_truncate(self, tmp_path: Path):
        """Initialize on a DB that has an extra non-MVP table — extra table survives."""
        db_path = str(tmp_path / "extra_table.db")
        engine = _create_test_engine(db_path)

        # Create an unrelated table first
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE other_data (payload TEXT)"))
            conn.execute(text("INSERT INTO other_data VALUES ('preserve-me')"))
            conn.commit()

        validate_and_initialize_schema(engine)

        # Both tables should exist
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "users" in tables
        assert "other_data" in tables

        # Data in other_data should survive
        with engine.connect() as conn:
            result = conn.execute(text("SELECT payload FROM other_data")).fetchone()
        assert result[0] == "preserve-me"
        engine.dispose()

    def test_no_ddl_alteration_on_existing_target(self, tmp_path: Path):
        """Initialization must not execute any table/index DDL on a compatible
        existing target.

        T-019 semantics: a legacy (unstamped) schema that structurally matches
        head gets exactly ONE new object — the ``alembic_version`` stamp.
        Stamped restarts are pure no-ops afterwards.

        Regression: previous _migrate_friend_request_constraint would DROP
        uq_pair_state and CREATE ix_active_pair on every startup.
        """
        db_path = str(tmp_path / "no_ddl.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)

        def _schema_snapshot():
            with engine.connect() as conn:
                return {
                    (r[0], r[1]): r[2]
                    for r in conn.execute(
                        text(
                            "SELECT type, name, sql FROM sqlite_master "
                            "WHERE type IN ('table', 'index') ORDER BY name"
                        )
                    ).fetchall()
                }

        before = _schema_snapshot()

        # First init pass: structure matches head -> stamped at head
        validate_and_initialize_schema(engine)
        after_first = _schema_snapshot()

        # The only new object allowed is the alembic_version stamp table
        added = set(after_first) - set(before)
        removed = set(before) - set(after_first)
        assert removed == set(), f"Init dropped schema objects: {removed}"
        # (the stamp table plus SQLite's autoindex on its primary key)
        assert added <= {
            ("table", "alembic_version"),
            ("index", "sqlite_autoindex_alembic_version_1"),
        }, f"Unexpected schema objects added: {added}"
        assert ("table", "alembic_version") in added

        # Second init pass (real restart): stamped DB must be byte-identical
        validate_and_initialize_schema(engine)
        after_second = _schema_snapshot()
        assert after_first == after_second, (
            "Stamped restart mutated the schema — DDL was executed. "
            f"Difference: {set(after_first) ^ set(after_second)}"
        )
        engine.dispose()


# ---------------------------------------------------------------------------
# T-019: Alembic is the single schema authority on SQLite
# ---------------------------------------------------------------------------

class TestT019AlembicAuthority:
    def test_fresh_db_is_created_by_alembic_and_stamped_at_head(self, tmp_path: Path):
        """A brand-new file is populated by `alembic upgrade head` (not
        create_all) and carries the head revision stamp."""
        from ting_ting.database import _alembic_head_revision

        db_path = str(tmp_path / "alembic_fresh.db")
        engine = _create_test_engine(db_path)

        validate_and_initialize_schema(engine)

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for expected in ("users", "posts", "sessions", "alembic_version"):
            assert expected in tables
        # every mapped table exists
        from ting_ting.models import Base
        assert set(Base.metadata.tables) <= tables

        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == _alembic_head_revision()
        engine.dispose()

    def test_stamped_db_behind_head_upgrades_preserving_data(self, tmp_path: Path):
        """A stamped database at an older revision is upgraded to head on
        startup without losing data (the 0007 posts rebuild must keep rows)."""
        from sqlalchemy.orm import sessionmaker

        from ting_ting.database import (
            _alembic_head_revision,
            _with_database_url,
        )
        from ting_ting.models import Comment, Like, Post

        db_path = str(tmp_path / "alembic_behind.db")
        engine = _create_test_engine(db_path)

        # Build the DB at revision 0005 (pre-sessions, pre-posts-cascade)
        with _with_database_url(engine):
            from ting_ting.database import _alembic_config
            from alembic import command as _alembic_command
            _alembic_command.upgrade(_alembic_config(), "20260819_0005")

        sm = sessionmaker(bind=engine, expire_on_commit=False)
        # Seed the user with raw SQL: the 0005 schema predates columns added
        # by later migrations (e.g. deactivated_at in 0009), so the current
        # ORM model would reference columns that do not exist yet.
        with sm() as s:
            s.execute(
                text(
                    "INSERT INTO users (username, email, password_hash, is_moderator) "
                    "VALUES ('survivor', 'survivor@example.com', "
                    "'$2b$12$fakehashfordata', 0)"
                )
            )
            author_id = s.execute(
                text("SELECT id FROM users WHERE username = 'survivor'")
            ).scalar()
            post = Post(author_id=author_id, content="keep me", audience="PUBLIC")
            s.add(post)
            s.flush()
            s.add(Like(user_id=author_id, post_id=post.id))
            s.add(Comment(post_id=post.id, author_id=author_id, content="child"))
            s.commit()
            post_id = post.id

        # Restart on an older revision -> validates + upgrades to head
        validate_and_initialize_schema(engine)

        from ting_ting.database import _current_revision
        assert _current_revision(engine) == _alembic_head_revision()

        with sm() as s:
            rows = s.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM likes WHERE post_id=:p), "
                    "(SELECT COUNT(*) FROM comments WHERE post_id=:p), "
                    "(SELECT content FROM posts WHERE id=:p)"
                ),
                {"p": post_id},
            ).one()
        assert rows[0] == 1 and rows[1] == 1 and rows[2] == "keep me"
        engine.dispose()

    def test_legacy_unstamped_matching_db_is_stamped_at_head(self, tmp_path: Path):
        """Legacy create_all schema (structurally == head) + data -> stamped
        at head, data preserved, no table DDL (covered alongside data check)."""
        from sqlalchemy.orm import sessionmaker

        from ting_ting.database import _alembic_head_revision, _current_revision

        db_path = str(tmp_path / "legacy.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)

        sm = sessionmaker(bind=engine, expire_on_commit=False)
        with sm() as s:
            u = User(
                username="legacy", email="legacy@example.com",
                password_hash="$2b$12$fakehashfordata",
            )
            s.add(u)
            s.commit()

        validate_and_initialize_schema(engine)

        assert _current_revision(engine) == _alembic_head_revision()
        with sm() as s:
            assert s.get(User, u.id) is not None
        engine.dispose()

    def test_legacy_unstamped_drifted_db_fails_closed(self, tmp_path: Path):
        """An unstamped schema that drifted from head (missing `sessions`)
        is refused BEFORE any mutation — no alembic_version gets created."""
        db_path = str(tmp_path / "drifted.db")
        engine = _create_test_engine(db_path)
        _init_test_engine(engine)
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE sessions"))
            conn.commit()

        with pytest.raises(ValueError, match="not Alembic-stamped"):
            validate_and_initialize_schema(engine)

        inspector = inspect(engine)
        assert "alembic_version" not in set(inspector.get_table_names())
        assert "sessions" not in set(inspector.get_table_names())
        engine.dispose()

    def test_stamped_db_at_head_is_a_noop(self, tmp_path: Path):
        """Restart on a current database performs schema reads only."""
        db_path = str(tmp_path / "at_head.db")
        engine = _create_test_engine(db_path)
        validate_and_initialize_schema(engine)

        def _snapshot():
            with engine.connect() as conn:
                return sorted(
                    f"{r[0]}/{r[1]}"
                    for r in conn.execute(
                        text("SELECT type, name FROM sqlite_master ORDER BY name")
                    ).fetchall()
                )

        before = _snapshot()
        validate_and_initialize_schema(engine)
        assert before == _snapshot()
        engine.dispose()
