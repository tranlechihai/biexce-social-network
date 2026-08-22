"""Unit tests for the notification service and integrity constraints.

Covers:
* record — self-skip, retry dedup, re-notify after read, invalid kind
* list_notifications — ordering, keyset cursor pagination, kind filter,
  block filtering
* unread_count / mark_read / mark_all_read
* DB integrity: self-follow, invalid audience, post delete cascade
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ting_ting import notifications
from ting_ting.database import _create_test_engine, _init_test_engine
from ting_ting.auth import hash_password
from ting_ting.models import (
    Activity, Block, Comment, Follow, Like, Post, User,
)


def _make_user(db, username):
    user = User(
        username=username,
        email=f"{username}@tt.com",
        password_hash=hash_password("password123"),
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def db(tmp_db_path: str):
    engine = _create_test_engine(tmp_db_path)
    _init_test_engine(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def users(db):
    return (_make_user(db, "alice"), _make_user(db, "bob"), _make_user(db, "carol"))


@pytest.fixture
def post(db, users):
    alice = users[0]
    p = Post(author_id=alice.id, content="hello", audience="PUBLIC")
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

class TestRecord:

    def test_self_action_is_skipped(self, db, users, post):
        alice = users[0]
        result = notifications.record(db, alice.id, alice.id, "like", post.id)
        assert result is None
        assert db.scalar(select(Activity.id).limit(1)) is None

    def test_creates_row(self, db, users, post):
        alice, bob = users[0], users[1]
        row = notifications.record(db, bob.id and alice.id, bob.id, "like", post.id)
        db.commit()
        assert row.user_id == alice.id
        assert row.actor_id == bob.id
        assert row.kind == "like"
        assert row.post_id == post.id
        assert row.read_at is None

    def test_invalid_kind_rejected(self, db, users, post):
        with pytest.raises(ValueError):
            notifications.record(db, users[0].id, users[1].id, "hype", post.id)

    def test_retry_dedupes_while_unread(self, db, users, post):
        first = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        second = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        assert first.id == second.id
        count = len(db.scalars(select(Activity)).all())
        assert count == 1

    def test_renotifies_after_read(self, db, users, post):
        first = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        db.commit()
        from datetime import datetime, timezone
        first.read_at = datetime.now(timezone.utc)
        db.commit()
        second = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        db.commit()
        assert second.id != first.id

    def test_follow_notification_has_no_post(self, db, users):
        row = notifications.record(db, users[0].id, users[1].id, "follow")
        db.commit()
        assert row.post_id is None

    def test_distinct_comment_sources_are_not_collapsed(self, db, users, post):
        first = notifications.record(
            db, users[0].id, users[1].id, "comment", post.id,
            source_key="comment:1",
        )
        second = notifications.record(
            db, users[0].id, users[1].id, "comment", post.id,
            source_key="comment:2",
        )
        assert first.id != second.id


class TestPreferences:

    def test_missing_row_defaults_all_enabled(self, db, users):
        values = notifications.get_preferences(db, users[0].id)
        assert set(values) == set(notifications.NOTIFICATION_KINDS)
        assert all(values.values())

    def test_partial_update_gates_future_events_not_history(self, db, users, post):
        existing = notifications.record(
            db, users[0].id, users[1].id, "like", post.id,
            source_key="post:old",
        )
        notifications.update_preferences(db, users[0].id, {"like": False})
        assert notifications.record(
            db, users[0].id, users[2].id, "like", post.id,
            source_key="post:new",
        ) is None
        assert notifications.record(
            db, users[0].id, users[2].id, "comment", post.id,
            source_key="comment:1",
        ) is not None
        assert db.get(Activity, existing.id) is not None

        notifications.update_preferences(db, users[0].id, {"like": True})
        assert notifications.record(
            db, users[0].id, users[2].id, "like", post.id,
            source_key="post:new",
        ) is not None


# ---------------------------------------------------------------------------
# list + cursor pagination
# ---------------------------------------------------------------------------

class TestListAndCursor:

    def _seed(self, db, users, n):
        # Distinct (actor, kind) pairs — the service dedupes per pair.
        combos = [
            (1, "like"), (2, "like"), (1, "comment"), (2, "comment"), (1, "follow"),
        ][:n]
        for i, (actor_idx, kind) in enumerate(combos):
            row = notifications.record(db, users[0].id, users[actor_idx].id, kind)
            row.created_at = row.created_at.replace(microsecond=i)
            db.flush()
        db.commit()

    def test_newest_first_with_cursor_walk(self, db, users):
        self._seed(db, users, 5)
        seen = []
        cursor = None
        while True:
            rows, cursor = notifications.list_notifications(
                db, users[0].id, limit=2, cursor=cursor,
            )
            seen.extend(r.id for r in rows)
            if cursor is None:
                break
        assert len(seen) == 5
        assert len(set(seen)) == 5
        created = [
            db.get(Activity, rid).created_at for rid in seen
        ]
        assert created == sorted(created, reverse=True)

    def test_kind_filter(self, db, users, post):
        notifications.record(db, users[0].id, users[1].id, "like", post.id)
        notifications.record(db, users[0].id, users[2].id, "comment", post.id)
        db.commit()
        rows, _ = notifications.list_notifications(
            db, users[0].id, kind="comment",
        )
        assert [r.kind for r in rows] == ["comment"]

    def test_invalid_kind_rejected(self, db, users):
        with pytest.raises(ValueError):
            notifications.list_notifications(db, users[0].id, kind="bogus")

    def test_blocked_by_me_hides_actor(self, db, users):
        alice, bob, carol = users
        notifications.record(db, alice.id, bob.id, "like")
        notifications.record(db, alice.id, carol.id, "comment")
        db.add(Block(blocker_id=alice.id, blocked_id=bob.id))
        db.commit()

        rows, _ = notifications.list_notifications(db, alice.id, limit=10)
        assert [r.actor_id for r in rows] == [carol.id]
        assert notifications.unread_count(db, alice.id) == 1

    def test_blocked_by_them_hides_actor(self, db, users):
        alice, bob, carol = users
        notifications.record(db, alice.id, bob.id, "like")
        notifications.record(db, alice.id, carol.id, "comment")
        db.add(Block(blocker_id=carol.id, blocked_id=alice.id))
        db.commit()

        rows, _ = notifications.list_notifications(db, alice.id, limit=10)
        assert [r.actor_id for r in rows] == [bob.id]
        assert notifications.unread_count(db, alice.id) == 1


# ---------------------------------------------------------------------------
# read state
# ---------------------------------------------------------------------------

class TestReadState:

    def test_unread_count(self, db, users, post):
        notifications.record(db, users[0].id, users[1].id, "like", post.id)
        notifications.record(db, users[0].id, users[2].id, "comment", post.id)
        db.commit()
        assert notifications.unread_count(db, users[0].id) == 2

    def test_mark_read_single(self, db, users, post):
        row = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        db.commit()
        assert notifications.mark_read(db, users[0].id, row) is True
        db.commit()
        assert row.read_at is not None
        assert notifications.unread_count(db, users[0].id) == 0
        # idempotent — already read
        assert notifications.mark_read(db, users[0].id, row) is False

    def test_mark_read_requires_owner(self, db, users, post):
        row = notifications.record(db, users[0].id, users[1].id, "like", post.id)
        db.commit()
        assert notifications.mark_read(db, users[2].id, row) is False

    def test_mark_all_read(self, db, users, post):
        notifications.record(db, users[0].id, users[1].id, "like", post.id)
        notifications.record(db, users[0].id, users[2].id, "comment", post.id)
        db.commit()
        updated = notifications.mark_all_read(db, users[0].id)
        db.commit()
        assert updated == 2
        assert notifications.unread_count(db, users[0].id) == 0
        rows, _ = notifications.list_notifications(db, users[0].id)
        assert all(r.read_at is not None for r in rows)


class TestAggregation:

    def test_group_counts_actors_events_and_cutoff_read(self, db, users, post):
        alice, bob, carol = users
        notifications.record(
            db, alice.id, bob.id, "comment", post.id, source_key="comment:1",
        )
        notifications.record(
            db, alice.id, bob.id, "comment", post.id, source_key="comment:2",
        )
        notifications.record(
            db, alice.id, carol.id, "comment", post.id, source_key="comment:3",
        )
        db.commit()

        groups = notifications.list_aggregates(db, alice.id)
        assert len(groups) == 1
        group = groups[0]
        assert group.kind == "comment" and group.post_id == post.id
        assert group.actor_count == 2 and group.event_count == 3
        assert {u.id for u in group.actors} == {bob.id, carol.id}

        # Event arriving after the aggregate was rendered must stay unread.
        later = notifications.record(
            db, alice.id, carol.id, "comment", post.id, source_key="comment:4",
        )
        db.commit()
        assert notifications.mark_aggregate_read(
            db, alice.id, group.aggregation_key,
        ) == 3
        db.commit()
        assert later.read_at is None
        assert notifications.unread_count(db, alice.id) == 1


# ---------------------------------------------------------------------------
# Integrity constraints
# ---------------------------------------------------------------------------

class TestIntegrity:

    def test_self_follow_rejected(self, db, users):
        alice = users[0]
        db.add(Follow(follower_id=alice.id, followed_id=alice.id))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_invalid_audience_rejected(self, db, users):
        alice = users[0]
        db.add(Post(author_id=alice.id, content="x", audience="PUBLIC"))
        db.flush()
        alice_post = db.scalars(select(Post)).one()
        alice_post.audience = "EVERYONE"
        with pytest.raises(IntegrityError):
            db.flush()

    def test_deleting_post_cascades_to_children(self, db, users, post):
        alice, bob = users[0], users[1]
        db.add(Like(user_id=bob.id, post_id=post.id))
        db.add(Comment(post_id=post.id, author_id=bob.id, content="hi"))
        notifications.record(db, alice.id, bob.id, "like", post.id)
        notifications.record(db, alice.id, bob.id, "comment", post.id)
        db.commit()
        post_id = post.id

        for obj in [r for r in db.scalars(select(Post)).all()]:
            from ting_ting.posts import delete_post
            if obj.id == post_id:
                delete_post(db, obj, alice.id)
        db.commit()

        assert db.scalar(select(Post.id).where(Post.id == post_id)) is None
        assert db.scalar(select(Like.id).where(Like.post_id == post_id)) is None
        assert db.scalar(select(Comment.id).where(Comment.post_id == post_id)) is None
        assert db.scalar(select(Activity.id).where(Activity.post_id == post_id)) is None

    def test_unread_source_key_unique_but_read_history_allowed(self, db, users, post):
        first = Activity(
            user_id=users[0].id, actor_id=users[1].id, kind="like",
            post_id=post.id, source_key="post:unique",
        )
        db.add(first)
        db.commit()
        db.add(Activity(
            user_id=users[0].id, actor_id=users[1].id, kind="like",
            post_id=post.id, source_key="post:unique",
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

        first = db.get(Activity, first.id)
        from datetime import datetime, timezone
        first.read_at = datetime.now(timezone.utc)
        db.commit()
        db.add(Activity(
            user_id=users[0].id, actor_id=users[1].id, kind="like",
            post_id=post.id, source_key="post:unique",
        ))
        db.commit()
