"""Unit tests for scripts/reconcile.py (T-022).

The script is a plain-file CLI (not a package), so it is loaded by path.
Database state is built on a temp file DB through the app's own test
helpers; uploads are a temp directory.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reconcile.py"


@pytest.fixture
def reconcile():
    spec = importlib.util.spec_from_file_location("reconcile_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if "reconcile_under_test" in sys.modules:  # keep re-imports fresh
        del sys.modules["reconcile_under_test"]
    return module


@pytest.fixture
def env(tmp_path, reconcile):
    """Temp DB (app schema) + temp uploads dir wired to run()."""
    from ting_ting.database import _create_test_engine, _init_test_engine

    db_path = tmp_path / "reconcile.db"
    engine = _create_test_engine(str(db_path))
    _init_test_engine(engine)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    return {"reconcile": reconcile, "engine": engine, "uploads": uploads,
            "db_url": f"sqlite:///{db_path}"}


def _args(env, fix=False):
    return argparse.Namespace(
        db_url=env["db_url"], uploads_dir=str(env["uploads"]), fix=fix,
    )


def test_missing_and_orphans_detected(env, capsys):
    from sqlalchemy.orm import Session as DBSession

    from ting_ting.models import Post, PostMedia, User, UserProfile

    with DBSession(env["engine"]) as db:
        author = User(username="auth", email="a@e.com", password_hash="x")
        db.add(author)
        db.flush()
        post = Post(author_id=author.id, content="p", audience="PUBLIC")
        db.add(post)
        db.flush()
        # Referenced file exists.
        (env["uploads"] / "ok.png").write_bytes(b"x")
        db.add(PostMedia(post_id=post.id, path="/media/ok.png", media_type="image"))
        # Referenced file is MISSING on disk.
        db.add(PostMedia(post_id=post.id, path="/media/gone.png", media_type="image"))
        # Avatar: local path referenced + one external URL (must be ignored).
        (env["uploads"] / "avatar.png").write_bytes(b"x")
        profile = UserProfile(user_id=author.id, avatar_path="/media/avatar.png",
                              avatar_url="https://example.com/remote.png")
        db.add(profile)
        db.commit()

    # Orphan on disk, referenced by nothing.
    (env["uploads"] / "orphan.png").write_bytes(b"x")

    rc = env["reconcile"].run(_args(env))
    out = capsys.readouterr().out

    assert rc == 1  # missing file present
    assert "gone.png" in out
    assert "ORPHAN   orphan.png" in out
    assert "ok.png" not in out
    assert "avatar.png" not in out
    assert "remote.png" not in out


def test_fix_removes_orphans(env, capsys):
    from sqlalchemy.orm import Session as DBSession

    from ting_ting.models import Post, PostMedia, User

    with DBSession(env["engine"]) as db:
        author = User(username="auth", email="a@e.com", password_hash="x")
        db.add(author)
        db.flush()
        post = Post(author_id=author.id, content="p", audience="PUBLIC")
        db.add(post)
        db.flush()
        (env["uploads"] / "ok.png").write_bytes(b"x")
        db.add(PostMedia(post_id=post.id, path="/media/ok.png", media_type="image"))
        db.commit()

    (env["uploads"] / "orphan.png").write_bytes(b"x")

    rc = env["reconcile"].run(_args(env, fix=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "removed 1 orphan file(s)" in out
    assert not (env["uploads"] / "orphan.png").exists()
    assert (env["uploads"] / "ok.png").exists()


def test_consistent_store_is_ok(env, capsys):
    rc = env["reconcile"].run(_args(env))
    out = capsys.readouterr().out
    assert rc == 0
    assert "reconcile OK" in out


def test_missing_uploads_dir_is_usage_error(env, capsys):
    rc = env["reconcile"].run(argparse.Namespace(
        db_url=env["db_url"], uploads_dir=str(env["uploads"] / "nope"), fix=False,
    ))
    err = capsys.readouterr().err
    assert rc == 2
    assert "uploads dir not found" in err


def test_unreadable_db_is_usage_error(env, capsys):
    rc = env["reconcile"].run(argparse.Namespace(
        db_url="sqlite:////nonexistent_dir/x.db", uploads_dir=str(env["uploads"]),
        fix=False,
    ))
    err = capsys.readouterr().err
    # An empty-but-writable path would create a DB; a non-writable path must
    # map to exit 2, never a traceback.
    assert rc == 2
    assert "could not read database" in err
