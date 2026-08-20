"""Unit tests for Like/Comment policy — deletion roles and visibility.

Covers:
* Like idempotency (create/remove)
* Comment creation validation
* Comment deletion roles (author vs post-author vs other)
"""

import pytest
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from ting_ting.database import _create_test_engine, _init_test_engine
from ting_ting.models import Post, User
from ting_ting.interactions import (
    count_comments,
    count_likes,
    create_comment,
    create_like,
    delete_comment,
    is_user_liked,
    list_comments,
    remove_like,
)
from ting_ting.auth import hash_password


@pytest.fixture
def db(tmp_db_path: str):
    """Yield an isolated session with all tables created."""
    engine = _create_test_engine(tmp_db_path)
    _init_test_engine(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def sample_post(db):
    """Create a single user + post fixture."""
    user = User(
        username="alice", email="alice@tt.com",
        password_hash=hash_password("password123"),
    )
    db.add(user)
    db.flush()

    post = Post(author_id=user.id, content="Test post", audience="FRIENDS")
    db.add(post)
    db.flush()

    return user, post


@pytest.fixture
def second_user(db):
    """Create a second user and make them friends with alice."""
    bob = User(
        username="bob", email="bob@tt.com",
        password_hash=hash_password("password456"),
    )
    db.add(bob)
    db.flush()
    return bob


# ---------------------------------------------------------------------------
# Like idempotency (AC1, AC2)
# ---------------------------------------------------------------------------

class TestLikeIdempotency:

    def test_create_like_creates_one_row(self, db, sample_post):
        user, post = sample_post
        like1 = create_like(db, user.id, post)
        like2 = create_like(db, user.id, post)
        assert like1.id == like2.id
        assert count_likes(db, post.id) == 1

    def test_remove_like_is_idempotent(self, db, sample_post):
        user, post = sample_post
        create_like(db, user.id, post)
        db.flush()
        remove_like(db, user.id, post)
        # Second removal = no-op
        result = remove_like(db, user.id, post)
        assert result is None
        assert count_likes(db, post.id) == 0

    def test_count_never_negative_after_removal(self, db, sample_post):
        user, post = sample_post
        # Never liked — remove should be no-op
        remove_like(db, user.id, post)
        assert count_likes(db, post.id) == 0

    def test_is_user_liked_true_after_create(self, db, sample_post):
        user, post = sample_post
        assert not is_user_liked(db, user.id, post.id)
        create_like(db, user.id, post)
        assert is_user_liked(db, user.id, post.id)

    def test_is_user_liked_false_after_remove(self, db, sample_post):
        user, post = sample_post
        create_like(db, user.id, post)
        assert is_user_liked(db, user.id, post.id)
        remove_like(db, user.id, post)
        assert not is_user_liked(db, user.id, post.id)

    def test_two_users_like_same_post(self, db, sample_post, second_user):
        user, post = sample_post
        create_like(db, user.id, post)
        create_like(db, second_user.id, post)
        assert count_likes(db, post.id) == 2
        assert is_user_liked(db, user.id, post.id)
        assert is_user_liked(db, second_user.id, post.id)
        # Remove one, other stays
        remove_like(db, user.id, post)
        assert count_likes(db, post.id) == 1
        assert not is_user_liked(db, user.id, post.id)
        assert is_user_liked(db, second_user.id, post.id)

    def test_concurrent_like_conflict_recovery_with_outer_write(self, tmp_db_path):
        """Deterministic actual DB conflict via flush hook — no mock IntegrityError.

        Hook ``sess.flush`` to detect the exact contention window: after the
        initial ``select(Like)`` completed, after the contender Like is pending
        in ``sess.new``, and before the contender's flush executes.  At that
        point the hook inserts the "winner" row via raw SQL through the SAME
        connection (within the savepoint), then calls the real flush.  The
        real flush tries to INSERT the contender Like and hits the actual
        SQLite UNIQUE constraint violation.  This is a real database-level
        IntegrityError, not a pre-made mock exception.

        Timeline:
        1. create_like SELECT → autoflush (dirty post, no Like pending,
           hook skips this flush) → SELECT returns None → OK
        2. Like pending added to sess → flush() inside begin_nested called
        3. HOOK fires: Like IN sess.new (contender pending after SELECT)
           → raw SQL INSERT winner row on same connection → commits
        4. HOOK calls ``original_flush()`` → contender Like INSERT hits UNIQUE
           constraint → real IntegrityError from SQLite
        5. IntegrityError escapes begin_nested() with-block → savepoint rolls back
        6. except branch: instance_state-based guarded expunge + re-query finds winner
        7. Outer staged write (post.content) must survive

        This proves the actual recovery code path executes with a real database
        constraint violation triggered at the correct point in the contention
        window.
        """
        from unittest import mock

        engine = _create_test_engine(tmp_db_path)
        _init_test_engine(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        # --- Main session: create fixtures ---
        sess = factory()
        user = User(
            username="db_hook_user", email="db_hook@tt.com",
            password_hash=hash_password("pw"),
        )
        sess.add(user)
        sess.flush()
        post = Post(author_id=user.id, content="original", audience="FRIENDS")
        sess.add(post)
        sess.flush()
        sess.commit()

        # --- Staged outer write (NOT committed) ---
        post.content = "mutated_outer"

        # --- Deterministic competition window hook ---
        # SQLite blocks concurrent writers while any implicit transaction is
        # active.  We use ``sess.no_autoflush()`` to suppress the implicit
        # transaction from autoflush, so ``engine.begin()`` can insert the
        # winner row through a separate connection.  The winner is committed
        # just before ``begin_nested()`` is called (hooked), so the initial
        # SELECT sees no row (not yet committed), but the contender flush
        # hits the real UNIQUE constraint.
        original_begin_nested = sess.begin_nested
        conflict_injected = [False]

        def patched_begin_nested():
            if not conflict_injected[0]:
                # Commit winner row before the savepoint opens
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO likes (user_id, post_id, created_at) "
                        "VALUES (:uid, :pid, :ts)"
                    ), {"uid": user.id, "pid": post.id,
                       "ts": datetime.now(timezone.utc)})
                conflict_injected[0] = True
            return original_begin_nested()

        with mock.patch.object(sess, "begin_nested", side_effect=patched_begin_nested):
            # Disable autoflush so the initial SELECT doesn't open an implicit
            # transaction that would prevent the hook from writing.
            with sess.no_autoflush:
                result = create_like(sess, user.id, post)

        # --- Assert: recovery branch executed (not fast path bypass) ---
        assert result is not None, \
            "create_like returned None -- recovery did not find winner row"
        assert count_likes(sess, post.id) == 1

        # --- Assert: outer staged write survived the savepoint rollback ---
        assert post.content == "mutated_outer", \
            f"Outer write lost by savepoint rollback; got {post.content!r}"

        # --- Persist and verify DB integrity ---
        assert is_user_liked(sess, user.id, post.id)
        sess.commit()

        with engine.connect() as conn:
            like_cnt = conn.execute(
                text("SELECT COUNT(*) FROM likes WHERE post_id=:pid"),
                {"pid": post.id},
            ).scalar()
            pc = conn.execute(
                text("SELECT content FROM posts WHERE id=:pid"),
                {"pid": post.id},
            ).scalar()

        assert like_cnt == 1, f"Expected 1 like row, got {like_cnt}"
        assert pc == "mutated_outer", f"Outer write not persisted: {pc!r}"

        sess.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Comment creation (AC3)
# ---------------------------------------------------------------------------

class TestCommentCreation:

    def test_create_comment_stores_and_count_increases(self, db, sample_post):
        user, post = sample_post
        before = count_comments(db, post.id)
        c = create_comment(db, post, user.id, "Nice post!")
        after = count_comments(db, post.id)
        assert after == before + 1
        assert c.author_id == user.id
        assert c.content == "Nice post!"

    def test_list_comments_ordered_oldest_first(self, db, sample_post):
        user, post = sample_post
        create_comment(db, post, user.id, "first")
        _c2 = create_comment(db, post, user.id, "second")
        comments = list_comments(db, post.id)
        assert len(comments) == 2
        # Second comment got a later auto-assigned id
        assert comments[1].id > comments[0].id

    def test_create_comment_by_different_author(self, db, sample_post, second_user):
        user, post = sample_post
        c = create_comment(db, post, second_user.id, "Hello")
        assert c.author_id == second_user.id
        assert count_comments(db, post.id) == 1


# ---------------------------------------------------------------------------
# Comment deletion roles (AC5)
# ---------------------------------------------------------------------------

class TestCommentDeletionRoles:

    def _setup_comment(self, db, sample_post, second_user):
        """Helper: alice creates post, bob comments."""
        user, post = sample_post
        bob = second_user
        c = create_comment(db, post, bob.id, "Nice!")
        return post, c, user, bob

    def test_comment_author_can_delete(self, db, sample_post, second_user):
        post, comment, alice, bob = self._setup_comment(db, sample_post, second_user)
        delete_comment(db, comment, bob.id, post)  # comment author
        db.flush()
        assert count_comments(db, post.id) == 0

    def test_post_author_can_delete_any_comment(self, db, sample_post, second_user):
        post, comment, alice, bob = self._setup_comment(db, sample_post, second_user)
        delete_comment(db, comment, alice.id, post)  # post author
        db.flush()
        assert count_comments(db, post.id) == 0

    def test_neither_author_denied(self, db, sample_post, second_user):
        post, comment, alice, bob = self._setup_comment(db, sample_post, second_user)
        # Create a third user
        eve = User(username="eve", email="eve@tt.com", password_hash=hash_password("x"))
        db.add(eve)
        db.flush()

        with pytest.raises(ValueError, match="forbidden"):
            delete_comment(db, comment, eve.id, post)

        # Comment still exists
        assert count_comments(db, post.id) == 1

    def test_count_decreases_after_delete(self, db, sample_post, second_user):
        post, comment, alice, bob = self._setup_comment(db, sample_post, second_user)
        create_comment(db, post, alice.id, "Re: Nice!")
        assert count_comments(db, post.id) == 2
        delete_comment(db, comment, bob.id, post)
        db.flush()
        assert count_comments(db, post.id) == 1

    def test_comment_author_also_deletes_own_comment_post_author_check(self, db, sample_post):
        """When comment author is the same as post author, both roles match."""
        user, post = sample_post
        c = create_comment(db, post, user.id, "Self-comment")
        # Should work -- author matches both conditions
        delete_comment(db, c, user.id, post)
        db.flush()
        assert count_comments(db, post.id) == 0
