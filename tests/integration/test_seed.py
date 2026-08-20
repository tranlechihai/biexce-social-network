"""Seed integration tests for AC6, AC7, AC8.

AC6: Fresh seed success — at least 3 users, friendship, both audiences, like, comment
AC7: Preflight refusal — incompatible schema or pre-populated DB
AC8: Transactional rollback — injected failure after writes
"""

import os
import pytest
from pathlib import Path

from sqlalchemy import create_engine, text

from ting_ting.models import Base, User
from ting_ting.seed import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _SeedHelpers:
    @staticmethod
    def _fresh_db(tmp_path: Path) -> str:
        """Create a fresh empty compatible DB and return its path."""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        engine.dispose()
        return db_path

    @staticmethod
    def _partial_db(tmp_path: Path) -> str:
        """Create a DB with only the users table (missing required tables for seed)."""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        User.__table__.create(engine)
        engine.dispose()
        return db_path

    @staticmethod
    def _count_records(db_path: str, table: str) -> int:
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        except Exception:
            return 0
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def demo_password_env():
    """Set TING_DEMO_PASSWORD for seed tests."""
    os.environ["TING_DEMO_PASSWORD"] = "seedtest123"
    yield
    os.environ.pop("TING_DEMO_PASSWORD", None)


# ---------------------------------------------------------------------------
# AC6: Fresh seed success
# ---------------------------------------------------------------------------

class TestAC6SeedSuccess:

    def test_seed_creates_three_users(self, tmp_path, demo_password_env):
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        assert _SeedHelpers._count_records(db_path, "users") == 3
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT username FROM users ORDER BY username")
                ).fetchall()
        finally:
            engine.dispose()
        usernames = [r[0] for r in rows]
        assert "alice" in usernames
        assert "bob" in usernames
        assert "carol" in usernames

    def test_seed_creates_friendship(self, tmp_path, demo_password_env):
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        friend_count = _SeedHelpers._count_records(db_path, "friend_requests")
        assert friend_count >= 1
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT state FROM friend_requests WHERE state = 'accepted'")
                ).fetchall()
        finally:
            engine.dispose()
        assert len(rows) >= 1

    def test_seed_creates_both_audiences(self, tmp_path, demo_password_env):
        """Seed creates posts with both ONLY_ME and FRIENDS audiences."""
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                friends_count = conn.execute(
                    text("SELECT COUNT(*) FROM posts WHERE audience = 'FRIENDS'")
                ).scalar()
                only_me_count = conn.execute(
                    text("SELECT COUNT(*) FROM posts WHERE audience = 'ONLY_ME'")
                ).scalar()
        finally:
            engine.dispose()
        assert friends_count >= 1
        assert only_me_count >= 1

    def test_seed_creates_like(self, tmp_path, demo_password_env):
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        like_count = _SeedHelpers._count_records(db_path, "likes")
        assert like_count >= 1

    def test_seed_creates_comment(self, tmp_path, demo_password_env):
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        comment_count = _SeedHelpers._count_records(db_path, "comments")
        assert comment_count >= 1

    def test_seed_demo_password_works(self, tmp_path, demo_password_env):
        """Verify that the seeded demo password is usable for login."""
        db_path = _SeedHelpers._fresh_db(tmp_path)
        run(db_path)
        from ting_ting.auth import verify_password
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                pw_hash = conn.execute(
                    text("SELECT password_hash FROM users WHERE username = 'alice'")
                ).scalar()
        finally:
            engine.dispose()
        assert verify_password("seedtest123", pw_hash)


# ---------------------------------------------------------------------------
# AC7: Preflight refusal
# ---------------------------------------------------------------------------

class TestAC7PreflightRefusal:

    def test_refuses_prepopulated_database(self, tmp_path, demo_password_env):
        """Seed refuses when target DB already has users."""
        db_path = _SeedHelpers._fresh_db(tmp_path)
        from ting_ting.auth import hash_password
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        from sqlalchemy.orm import sessionmaker
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        session.add(User(
            username="existing_user",
            email="existing@example.com",
            password_hash=hash_password("something1"),
        ))
        session.commit()
        session.close()
        engine.dispose()

        with pytest.raises(SystemExit) as exc_info:
            run(db_path)
        assert exc_info.value.code == 1

        # Existing user must remain unchanged
        assert _SeedHelpers._count_records(db_path, "users") == 1

    def test_refuses_unsupported_backend(self, demo_password_env):
        """Seed preflight refuses backends outside SQLite/PostgreSQL."""
        from ting_ting.seed import _preflight
        from unittest.mock import MagicMock

        fake_engine = MagicMock()
        fake_engine.name = "mysql"

        with pytest.raises(SystemExit) as exc_info:
            _preflight(fake_engine)
        assert exc_info.value.code == 1

    def test_refuses_missing_users_table(self, tmp_path, demo_password_env):
        """Seed refuses when users table is missing."""
        db_path = str(tmp_path / "empty.db")
        engine = create_engine(f"sqlite:///{db_path}")
        engine.dispose()

        with pytest.raises(SystemExit) as exc_info:
            run(db_path)
        assert exc_info.value.code == 1

    def test_refuses_missing_required_tables(self, tmp_path, demo_password_env):
        """Seed refuses when required tables (posts, likes, etc.) are missing."""
        db_path = _SeedHelpers._partial_db(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            run(db_path)
        assert exc_info.value.code == 1

    def test_refusal_no_mutation(self, tmp_path, demo_password_env):
        """On refusal, existing records remain field-for-field unchanged."""
        db_path = _SeedHelpers._fresh_db(tmp_path)
        from ting_ting.auth import hash_password
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        from sqlalchemy.orm import sessionmaker
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        original_hash = hash_password("original_pass")
        user = User(
            username="immutable_user",
            email="immutable@example.com",
            password_hash=original_hash,
            display_name="Immutable",
            bio="Should not change",
        )
        session.add(user)
        session.commit()
        session.close()
        engine.dispose()

        with pytest.raises(SystemExit):
            run(db_path)

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT * FROM users WHERE username = 'immutable_user'")
                ).fetchone()
        finally:
            engine.dispose()
        assert row is not None
        # Row: (id, username, email, password_hash, display_name, bio)
        assert row[1] == "immutable_user"
        assert row[2] == "immutable@example.com"
        assert row[3] == original_hash
        assert row[4] == "Immutable"
        assert row[5] == "Should not change"


# ---------------------------------------------------------------------------
# AC8: Transactional rollback
# ---------------------------------------------------------------------------

class TestAC8TransactionRollback:

    def test_rollback_on_injected_failure(self, tmp_path, demo_password_env):
        """Simulated failure mid-write rolls back everything."""
        db_path = _SeedHelpers._fresh_db(tmp_path)

        import sqlalchemy.orm as sa_orm

        call_count = [0]
        orig_commit = sa_orm.Session.commit

        def failing_commit(self):
            call_count[0] += 1
            if call_count[0] == 1:
                sa_orm.Session.rollback(self)
                raise RuntimeError("INJECTED_FAILURE_FOR_ROLLBACK_TEST")
            return orig_commit(self)

        try:
            sa_orm.Session.commit = failing_commit
            with pytest.raises((RuntimeError, SystemExit)):
                run(db_path)
        finally:
            sa_orm.Session.commit = orig_commit

        # After rollback: all tables should be empty
        assert _SeedHelpers._count_records(db_path, "users") == 0
        assert _SeedHelpers._count_records(db_path, "friend_requests") == 0
        assert _SeedHelpers._count_records(db_path, "posts") == 0
        assert _SeedHelpers._count_records(db_path, "likes") == 0
        assert _SeedHelpers._count_records(db_path, "comments") == 0
