"""Unit tests for post audience authorization policy.

Cover visibility rules at the service layer using a real DB session.
"""

import pytest

from ting_ting.auth import hash_password
from ting_ting.models import User, Post
from ting_ting import social, posts


@pytest.fixture
def users_and_session(tmp_session):
    """Three users + one pair of friends + one blocked pair + one unrelated."""
    user_a = User(username="pol_a", email="a@p.com", password_hash=hash_password("pass"))
    user_b = User(username="pol_b", email="b@p.com", password_hash=hash_password("pass"))
    user_c = User(username="pol_c", email="c@p.com", password_hash=hash_password("pass"))
    tmp_session.add_all([user_a, user_b, user_c])
    tmp_session.commit()
    tmp_session.refresh(user_a)
    tmp_session.refresh(user_b)
    tmp_session.refresh(user_c)

    # A and B are friends
    social.create_friend_request(tmp_session, user_a, user_b)
    req = tmp_session.query(social.FriendRequest).filter_by(
        sender_id=user_a.id, recipient_id=user_b.id,
    ).first()
    social.accept_friend_request(tmp_session, req, user_b)
    tmp_session.commit()

    return {
        "a": user_a,
        "b": user_b,
        "c": user_c,
        "db": tmp_session,
    }


@pytest.fixture
def blocked(users_and_session):
    """Block user_a → user_b (unfriend automatically happens)."""
    u = users_and_session
    social.block_user(u["db"], u["a"], u["b"])
    u["db"].commit()


class TestIsVisibleTo:

    def test_author_always_sees_own(self, users_and_session):
        a = users_and_session["a"]
        db = users_and_session["db"]
        assert posts.is_visible_to(a.id, a.id, "ONLY_ME", db)
        assert posts.is_visible_to(a.id, a.id, "FRIENDS", db)

    def test_only_me_denies_non_author(self, users_and_session):
        """Author's ONLY_ME post: friend, stranger, all denied."""
        u = users_and_session
        db = u["db"]
        # B is A's friend, C is unrelated
        for viewer in [u["b"], u["c"]]:
            assert posts.is_visible_to(u["a"].id, viewer.id, "ONLY_ME", db) is False

    def test_friends_allows_current_friend(self, users_and_session):
        """FRIENDS audience: current friend can read."""
        u = users_and_session
        db = u["db"]
        # A is B's friend (bidirectional via accepted request)
        assert posts.is_visible_to(u["a"].id, u["b"].id, "FRIENDS", db)
        assert posts.is_visible_to(u["b"].id, u["a"].id, "FRIENDS", db)

    def test_friends_denies_stranger(self, users_and_session):
        """FRIENDS audience: non-friend cannot read."""
        u = users_and_session
        assert posts.is_visible_to(u["a"].id, u["c"].id, "FRIENDS", u["db"]) is False

    def test_blocked_denies_only_me(self, users_and_session, blocked):
        """Blocked pair: ONLY_ME denied to blocked peer."""
        u = users_and_session
        assert posts.is_visible_to(u["a"].id, u["b"].id, "ONLY_ME", u["db"]) is False
        assert posts.is_visible_to(u["b"].id, u["a"].id, "ONLY_ME", u["db"]) is False

    def test_blocked_denies_friends(self, users_and_session, blocked):
        """Blocked pair: FRIENDS denied to blocked peer (even if formerly friends)."""
        u = users_and_session
        assert posts.is_visible_to(u["a"].id, u["b"].id, "FRIENDS", u["db"]) is False
        assert posts.is_visible_to(u["b"].id, u["a"].id, "FRIENDS", u["db"]) is False

    def test_unknown_audience_denies(self, users_and_session):
        """Unknown audience → deny."""
        u = users_and_session
        assert posts.is_visible_to(u["a"].id, u["b"].id, "public", u["db"]) is False
        assert posts.is_visible_to(u["a"].id, u["a"].id, "public", u["db"]) is True  # author always

    def test_unfriend_removes_visibility(self, users_and_session):
        """After unfriend, FRIENDS audience is no longer visible."""
        u = users_and_session
        social.unfriend(u["db"], u["a"].id, u["b"].id, u["a"])
        u["db"].commit()
        assert posts.is_visible_to(u["a"].id, u["b"].id, "FRIENDS", u["db"]) is False


class TestPostCRUD:

    def test_create_post(self, tmp_session):
        user = User(username="test_cr", email="cr@p.com", password_hash=hash_password("pass"))
        tmp_session.add(user)
        tmp_session.commit()
        tmp_session.refresh(user)

        post = posts.create_post(tmp_session, user.id, "hello world", "ONLY_ME")
        tmp_session.commit()
        assert post.id is not None
        assert post.content == "hello world"
        assert post.audience == "ONLY_ME"

    def test_edit_as_author(self, tmp_session):
        user = User(username="edit_a", email="ea@p.com", password_hash=hash_password("pass"))
        tmp_session.add(user)
        tmp_session.commit()
        tmp_session.refresh(user)

        post = posts.create_post(tmp_session, user.id, "original", "ONLY_ME")
        tmp_session.commit()

        posts.edit_post(tmp_session, post, user.id, content="updated")
        assert post.content == "updated"

    def test_edit_as_non_author_forbidden(self, tmp_session):
        a = User(username="non_a", email="na@p.com", password_hash=hash_password("pass"))
        b = User(username="non_b", email="nb@p.com", password_hash=hash_password("pass"))
        tmp_session.add_all([a, b])
        tmp_session.commit()
        tmp_session.refresh(a)
        tmp_session.refresh(b)

        post = posts.create_post(tmp_session, a.id, "content", "FRIENDS")
        with pytest.raises(ValueError, match="forbidden"):
            posts.edit_post(tmp_session, post, b.id, content="hacked")

    def test_delete_as_author(self, tmp_session):
        user = User(username="del_a", email="da@p.com", password_hash=hash_password("pass"))
        tmp_session.add(user)
        tmp_session.commit()
        tmp_session.refresh(user)

        post = posts.create_post(tmp_session, user.id, "bye", "ONLY_ME")
        tmp_session.commit()
        assert tmp_session.query(Post).filter_by(id=post.id).first() is not None

        posts.delete_post(tmp_session, post, user.id)
        tmp_session.commit()
        assert tmp_session.query(Post).filter_by(id=post.id).first() is None

    def test_delete_as_non_author_forbidden(self, tmp_session):
        a = User(username="del_b", email="db@p.com", password_hash=hash_password("pass"))
        b = User(username="del_c", email="dc@p.com", password_hash=hash_password("pass"))
        tmp_session.add_all([a, b])
        tmp_session.commit()
        tmp_session.refresh(a)
        tmp_session.refresh(b)

        post = posts.create_post(tmp_session, a.id, "content", "FRIENDS")
        with pytest.raises(ValueError, match="forbidden"):
            posts.delete_post(tmp_session, post, b.id)
