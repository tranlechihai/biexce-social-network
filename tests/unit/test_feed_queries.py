"""Unit tests for SQL feed queries — visibility, keyset cursors, following feed.

Covers:
* query_feed — own posts, PUBLIC, FRIENDS, ONLY_ME, blocks (both directions),
  malformed cursor fallback, cursor walk without duplicates
* query_following_feed — followed authors, reposts of followed users,
  invisible candidates excluded, dedup
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ting_ting import posts as posts_module
from ting_ting.auth import hash_password
from ting_ting.database import _create_test_engine, _init_test_engine
from ting_ting.models import (
    Block, Follow, FriendRequest, Post, Repost, User,
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
    """alice (viewer), bob, carol, dave."""
    return tuple(_make_user(db, name) for name in ("alice", "bob", "carol", "dave"))


def _post(db, author, audience, content, offset_seconds=0):
    now = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    p = Post(author_id=author.id, content=content, audience=audience)
    p.created_at = now
    p.updated_at = now
    db.add(p)
    db.flush()
    return p


def _accept_friendship(db, a, b):
    left, right = (min(a.id, b.id), max(a.id, b.id))
    db.add(FriendRequest(
        sender_id=a.id, recipient_id=b.id,
        canonical_left=left, canonical_right=right, state="accepted",
    ))
    db.flush()


def _follow(db, follower, followed):
    db.add(Follow(follower_id=follower.id, followed_id=followed.id))
    db.flush()


class TestQueryFeed:

    def test_own_posts_any_audience(self, db, users):
        alice, bob = users[0], users[1]
        _post(db, alice, "ONLY_ME", "a only", 0)
        _post(db, alice, "PUBLIC", "a pub", 1)
        _post(db, bob, "PUBLIC", "b pub", 2)
        db.commit()
        page, _ = posts_module.query_feed(db, alice.id, limit=10)
        assert {p.content for p in page} == {"a only", "a pub", "b pub"}

    def test_public_visible_to_all(self, db, users):
        alice, bob = users[0], users[1]
        _post(db, bob, "PUBLIC", "b pub", 0)
        _post(db, bob, "ONLY_ME", "b only", 1)
        db.commit()
        page, _ = posts_module.query_feed(db, alice.id, limit=10)
        assert [p.content for p in page] == ["b pub"]

    def test_friends_audience_requires_friendship(self, db, users):
        alice, bob, carol, dave = users
        _post(db, bob, "FRIENDS", "b friends", 0)
        _post(db, carol, "FRIENDS", "c friends", 1)
        _accept_friendship(db, alice, bob)
        db.commit()
        page, _ = posts_module.query_feed(db, alice.id, limit=10)
        # carol is NOT a friend of alice — her FRIENDS post stays hidden.
        assert [p.content for p in page] == ["b friends"]

    def test_block_bidirectional_hides_posts(self, db, users):
        alice, bob = users[0], users[1]
        _post(db, bob, "PUBLIC", "b pub", 0)
        _accept_friendship(db, alice, bob)
        _post(db, bob, "FRIENDS", "b friends", 1)
        db.commit()

        db.add(Block(blocker_id=alice.id, blocked_id=bob.id))
        db.commit()
        page, _ = posts_module.query_feed(db, alice.id, limit=10)
        assert page == []

        db.delete(db.scalars(select(Block)).one())
        db.add(Block(blocker_id=bob.id, blocked_id=alice.id))
        db.commit()
        page, _ = posts_module.query_feed(db, alice.id, limit=10)
        assert page == []

    def test_cursor_walk_no_dups_in_order(self, db, users):
        alice, bob, carol, dave = users
        for i, author in enumerate((bob, carol, dave, bob, carol, bob, dave)):
            _post(db, author, "PUBLIC", f"p{i}", i)
        db.commit()

        seen = []
        cursor = None
        while True:
            page, cursor = posts_module.query_feed(db, alice.id, limit=3, cursor=cursor)
            seen.extend(p.content for p in page)
            if cursor is None:
                break
        assert len(seen) == 7
        assert len(set(seen)) == 7
        assert seen == sorted(seen, reverse=True)

    def test_malformed_cursor_falls_back_to_first_page(self, db, users):
        alice, bob = users[0], users[1]
        for i in range(3):
            _post(db, bob, "PUBLIC", f"m{i}", i)
        db.commit()
        page, cursor = posts_module.query_feed(db, alice.id, limit=2, cursor="!!bad!!")
        assert len(page) == 2
        assert cursor is not None

    def test_no_more_page_returns_none_cursor(self, db, users):
        alice, bob = users[0], users[1]
        _post(db, bob, "PUBLIC", "only", 0)
        db.commit()
        page, cursor = posts_module.query_feed(db, alice.id, limit=10)
        assert len(page) == 1
        assert cursor is None


class TestQueryFollowingFeed:

    def test_followed_author_posts(self, db, users):
        alice, bob, carol, dave = users
        _follow(db, alice, bob)
        _post(db, bob, "PUBLIC", "bob pub", 0)
        _post(db, carol, "PUBLIC", "carol pub", 1)  # not followed
        db.commit()
        page, _ = posts_module.query_following_feed(db, alice.id, limit=10)
        assert [p.content for p in page] == ["bob pub"]

    def test_friends_between_followers(self, db, users):
        alice, bob = users[0], users[1]
        _follow(db, alice, bob)
        _accept_friendship(db, alice, bob)
        _post(db, bob, "FRIENDS", "bob friends", 0)
        _post(db, bob, "ONLY_ME", "bob only", 1)
        db.commit()
        page, _ = posts_module.query_following_feed(db, alice.id, limit=10)
        assert [p.content for p in page] == ["bob friends"]

    def test_repost_by_followed_user_of_invisible_post_stays_hidden(self, db, users):
        """Reposting does NOT grant visibility: carol (followed) reposts
        dave's FRIENDS post she can see; alice is not friends with dave, so
        the original stays invisible in alice's following feed."""
        alice, bob, carol, dave = users
        _follow(db, alice, carol)
        _accept_friendship(db, dave, carol)
        shared = _post(db, dave, "FRIENDS", "shared", 0)
        db.add(Repost(user_id=carol.id, post_id=shared.id))
        db.commit()
        page, _ = posts_module.query_following_feed(db, alice.id, limit=10)
        assert page == []

    def test_repost_of_public_post_becomes_candidate(self, db, users):
        """carol (followed) reposts dave's PUBLIC post; alice sees it in the
        following feed even though she does not follow dave."""
        alice, bob, carol, dave = users
        _follow(db, alice, carol)
        pub = _post(db, dave, "PUBLIC", "dave pub", 0)
        db.add(Repost(user_id=carol.id, post_id=pub.id))
        db.commit()
        page, _ = posts_module.query_following_feed(db, alice.id, limit=10)
        assert [p.content for p in page] == ["dave pub"]

    def test_repost_dedupes_candidates(self, db, users):
        """A post by a followed author that is ALSO reposted by a followed
        author appears exactly once."""
        alice, bob, carol, dave = users
        _follow(db, alice, bob)
        _follow(db, alice, carol)
        p = _post(db, bob, "PUBLIC", "once", 0)
        db.add(Repost(user_id=carol.id, post_id=p.id))
        db.commit()
        page, _ = posts_module.query_following_feed(db, alice.id, limit=10)
        assert [post.id for post in page] == [p.id]

    def test_cursor_walk(self, db, users):
        alice, bob, carol, dave = users
        _follow(db, alice, bob)
        _follow(db, alice, carol)
        for i in range(5):
            _post(db, bob if i % 2 else carol, "PUBLIC", f"f{i}", i)
        db.commit()
        seen = []
        cursor = None
        while True:
            page, cursor = posts_module.query_following_feed(
                db, alice.id, limit=2, cursor=cursor,
            )
            seen.extend(p.id for p in page)
            if cursor is None:
                break
        assert len(seen) == 5 == len(set(seen))
