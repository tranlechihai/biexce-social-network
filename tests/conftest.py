"""Pytest configuration and shared fixtures for Ting Ting tests.

All tests use isolated temporary SQLite databases to avoid cross-test
interference and to prove create-only schema initialization.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ting_ting.database import _create_test_engine, _init_test_engine
from ting_ting.main import app
from ting_ting.models import User


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

_JWT_SECRET = "test-secret-do-not-use-in-production"


@pytest.fixture(autouse=True, scope="session")
def _env_jwt_secret():
    """Ensure TING_JWT_SECRET is set for the entire test session."""
    os.environ["TING_JWT_SECRET"] = _JWT_SECRET
    os.environ["TING_COOKIE_SECURE"] = "false"
    os.environ["TING_DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["TING_RATE_LIMIT_ENABLED"] = "false"
    yield
    for key in (
        "TING_JWT_SECRET", "TING_COOKIE_SECURE", "TING_DATABASE_URL",
        "TING_RATE_LIMIT_ENABLED",
    ):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Isolated temporary database
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Path to a fresh temporary SQLite file."""
    return str(tmp_path / "test.db")


@pytest.fixture
def tmp_engine(tmp_db_path: str):
    """Create and initialize a temporary SQLite engine + tables."""
    engine = _create_test_engine(tmp_db_path)
    _init_test_engine(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def tmp_session(tmp_engine):
    """Yield a session bound to the temporary engine."""
    factory = sessionmaker(bind=tmp_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# TestClient with a temporary database (integration)
# ---------------------------------------------------------------------------

# Keep track of the current temp engine so we can swap the dependency.
_test_engine = None


class CsrfTestClient(TestClient):
    """Make web test mutations behave like forms rendered by the application."""

    def request(self, method, url, **kwargs):
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and str(url).startswith("/web/"):
            if not self.cookies.get("ting_ting_csrf"):
                super().request("GET", "/web/login")
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-CSRF-Token", self.cookies.get("ting_ting_csrf", ""))
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def admin_user(tmp_session):
    """A pre-created user for auth testing."""
    from ting_ting.auth import hash_password

    user = User(
        username="alice",
        email="alice@example.com",
        password_hash=hash_password("password123"),
        display_name="Alice Test",
        bio="Test user",
    )
    tmp_session.add(user)
    tmp_session.commit()
    tmp_session.refresh(user)
    return user


@pytest.fixture
def second_user(tmp_session):
    """A second user for cross-user auth tests."""
    from ting_ting.auth import hash_password

    user = User(
        username="bob",
        email="bob@example.com",
        password_hash=hash_password("password456"),
    )
    tmp_session.add(user)
    tmp_session.commit()
    tmp_session.refresh(user)
    return user


@pytest.fixture
def client(tmp_engine):
    """TestClient wired to use a temporary database engine."""

    # Override the global engine for the duration of the test
    import ting_ting.database as db_mod

    orig_engine = db_mod._engine
    orig_session = db_mod._SessionLocal

    try:
        db_mod._engine = tmp_engine
        db_mod._SessionLocal = sessionmaker(
            bind=tmp_engine, expire_on_commit=False
        )
        yield CsrfTestClient(app)
    finally:
        db_mod._engine = orig_engine
        db_mod._SessionLocal = orig_session
