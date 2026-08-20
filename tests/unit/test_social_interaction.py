"""Unit tests for Increment 4 services — comment replies, edit comment,
cancel sent request, mutes, user search, feed suppression queries.
"""

import pytest

from ting_ting.auth import hash_password
from ting_ting.models import (
    FriendRequest, Mute, Post, User,
)
from ting_ting import interactions, posts as posts_service, social


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def three_users(tmp_session):
    users = []
    for username in ("unit_a", "unit_b", "unit_c"):
        u = User(
            username=username,
            email=f"{username}@unit.com",
            password_hash=hash_password("pass1234"),
        )
        tmp_session.add(u)
        users.append(u)
    tmp_session.commit()
    for u in users:
        tmp_session.refresh(u)
    return users


@pytest.fixture
def visible_post(tmp_session, three_users):
    post = Post(author_id=three_users[0].id, content="hello", audience="PUBLIC")
    tmp_session.add(post)
    tmp_session.commit()
    tmp_session.refresh(post)
    return post


# ---------------------------------------------------------------------------
# Comment replies (parent validation)
# ---------------------------------------------------------------------------

class TestCreateCommentReply:
    def test_top_level_comment_has_no_parent(self, tmp_session, visible_post, three_users):
        comment = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "top",
        )
        assert comment.parent_comment_id is None

    def test_valid_reply(self, tmp_session, visible_post, three_users):
        parent = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "top",
        )
        reply = interactions.create_comment(
            tmp_session, visible_post, three_users[2].id, "reply",
            parent_comment_id=parent.id,
        )
        assert reply.parent_comment_id == parent.id
        assert reply.post_id == visible_post.id

    def test_missing_parent_rejected(self, tmp_session, visible_post, three_users):
        with pytest.raises(ValueError, match="parent_not_found"):
            interactions.create_comment(
                tmp_session, visible_post, three_users[1].id, "x",
                parent_comment_id=99999,
            )

    def test_parent_on_other_post_rejected(self, tmp_session, visible_post, three_users):
        other = Post(author_id=three_users[1].id, content="other", audience="PUBLIC")
        tmp_session.add(other)
        tmp_session.commit()
        tmp_session.refresh(other)
        parent = interactions.create_comment(tmp_session, other, three_users[1].id, "o")
        with pytest.raises(ValueError, match="parent_mismatch"):
            interactions.create_comment(
                tmp_session, visible_post, three_users[2].id, "x",
                parent_comment_id=parent.id,
            )

    def test_reply_to_reply_rejected(self, tmp_session, visible_post, three_users):
        a = three_users
        parent = interactions.create_comment(tmp_session, visible_post, a[1].id, "top")
        reply = interactions.create_comment(
            tmp_session, visible_post, a[2].id, "mid", parent_comment_id=parent.id,
        )
        with pytest.raises(ValueError, match="reply_to_reply"):
            interactions.create_comment(
                tmp_session, visible_post, a[1].id, "deep", parent_comment_id=reply.id,
            )

    def test_deleting_parent_cascades_replies(self, tmp_session, visible_post, three_users):
        parent = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "top",
        )
        interactions.create_comment(
            tmp_session, visible_post, three_users[2].id, "reply",
            parent_comment_id=parent.id,
        )
        tmp_session.refresh(parent)
        interactions.delete_comment(tmp_session, parent, three_users[1].id, visible_post)
        tmp_session.commit()
        tmp_session.expire_all()
        comments = interactions.list_comments(tmp_session, visible_post.id)
        assert [c.content for c in comments] == ["top"] or all(
            c.content != "reply" for c in comments
        )


# ---------------------------------------------------------------------------
# Edit comment
# ---------------------------------------------------------------------------

class TestEditComment:
    def test_author_can_edit(self, tmp_session, visible_post, three_users):
        comment = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "before",
        )
        interactions.edit_comment(tmp_session, comment, three_users[1].id, "after")
        tmp_session.flush()
        assert comment.content == "after"

    def test_non_author_cannot_edit(self, tmp_session, visible_post, three_users):
        comment = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "text",
        )
        with pytest.raises(ValueError, match="forbidden"):
            interactions.edit_comment(tmp_session, comment, three_users[2].id, "hack")

    def test_post_author_cannot_edit_others_comment(
        self, tmp_session, visible_post, three_users,
    ):
        comment = interactions.create_comment(
            tmp_session, visible_post, three_users[1].id, "text",
        )
        with pytest.raises(ValueError, match="forbidden"):
            interactions.edit_comment(
                tmp_session, comment, visible_post.author_id, "hack",
            )


# ---------------------------------------------------------------------------
# Cancel sent request
# ---------------------------------------------------------------------------

class TestCancelSentRequest:
    def _pending(self, tmp_session, a, b):
        return social.create_friend_request(tmp_session, a, b)

    def test_sender_can_cancel(self, tmp_session, three_users):
        a, b, _ = three_users
        req = self._pending(tmp_session, a, b)
        social.cancel_sent_request(tmp_session, req, a)
        tmp_session.commit()
        assert tmp_session.get(FriendRequest, req.id) is None

    def test_recipient_cannot_cancel(self, tmp_session, three_users):
        a, b, _ = three_users
        req = self._pending(tmp_session, a, b)
        with pytest.raises(ValueError, match="not_sender"):
            social.cancel_sent_request(tmp_session, req, b)

    def test_cancel_non_pending_rejected(self, tmp_session, three_users):
        a, b, _ = three_users
        req = self._pending(tmp_session, a, b)
        social.accept_friend_request(tmp_session, req, b)
        with pytest.raises(ValueError, match="not_pending"):
            social.cancel_sent_request(tmp_session, req, a)


# ---------------------------------------------------------------------------
# Mutes
# ---------------------------------------------------------------------------

class TestMutes:
    def test_mute_idempotent(self, tmp_session, three_users):
        a, b, _ = three_users
        first = social.mute_user(tmp_session, a, b.id)
        second = social.mute_user(tmp_session, a, b.id)
        assert first.id == second.id

    def test_self_mute_rejected(self, tmp_session, three_users):
        a = three_users[0]
        with pytest.raises(ValueError, match="self_mute"):
            social.mute_user(tmp_session, a, a.id)

    def test_unmute(self, tmp_session, three_users):
        a, b, _ = three_users
        social.mute_user(tmp_session, a, b.id)
        assert social.is_muted_by(tmp_session, a.id, b.id)
        assert social.unmute_user(tmp_session, a, b.id) is True
        assert social.is_muted_by(tmp_session, a.id, b.id) is False
        assert social.unmute_user(tmp_session, a, b.id) is False

    def test_mute_is_directional(self, tmp_session, three_users):
        a, b, _ = three_users
        social.mute_user(tmp_session, a, b.id)
        assert social.is_muted_by(tmp_session, b.id, a.id) is False


# ---------------------------------------------------------------------------
# Feed suppression (muted author + hidden post excluded in SQL)
# ---------------------------------------------------------------------------

class TestFeedSuppression:
    def _public_post(self, tmp_session, user, tag):
        post = Post(author_id=user.id, content=tag, audience="PUBLIC")
        tmp_session.add(post)
        tmp_session.commit()
        tmp_session.refresh(post)
        return post

    def test_muted_author_excluded_from_for_you_feed(self, tmp_session, three_users):
        a, b, _ = three_users
        target = self._public_post(tmp_session, b, "from-b")
        social.mute_user(tmp_session, a, b.id)
        page, _ = posts_service.query_feed(tmp_session, a.id, limit=10)
        assert target.id not in [p.id for p in page]
        # Unmute restores visibility.
        social.unmute_user(tmp_session, a, b.id)
        tmp_session.commit()
        page, _ = posts_service.query_feed(tmp_session, a.id, limit=10)
        assert target.id in [p.id for p in page]

    def test_muted_author_excluded_from_following_feed(self, tmp_session, three_users):
        from ting_ting.models import Follow
        a, b, c = three_users
        from_b = self._public_post(tmp_session, b, "from-b")
        from_c = self._public_post(tmp_session, c, "from-c")
        tmp_session.add(Follow(follower_id=a.id, followed_id=b.id))
        tmp_session.add(Follow(follower_id=a.id, followed_id=c.id))
        tmp_session.commit()
        social.mute_user(tmp_session, a, b.id)
        social.mute_user(tmp_session, a, c.id)
        page, _ = posts_service.query_following_feed(tmp_session, a.id, limit=10)
        assert page == []
        social.unmute_user(tmp_session, a, b.id)
        social.unmute_user(tmp_session, a, c.id)
        tmp_session.commit()
        page, _ = posts_service.query_following_feed(tmp_session, a.id, limit=10)
        assert {p.id for p in page} == {from_b.id, from_c.id}

    def test_muted_post_excluded_from_feed_but_stored(self, tmp_session, three_users):
        a, b, _ = three_users
        target = self._public_post(tmp_session, b, "hide-me")
        tmp_session.add(Mute(muted_by=a.id, post_id=target.id))
        tmp_session.commit()
        page, _ = posts_service.query_feed(tmp_session, a.id, limit=10)
        assert target.id not in [p.id for p in page]
        # The post row and its content are intact.
        assert tmp_session.get(Post, target.id).content == "hide-me"


# ---------------------------------------------------------------------------
# User search (keyset)
# ---------------------------------------------------------------------------

class TestUserSearch:
    def test_excludes_viewer_and_blocked_by_them(self, tmp_session, three_users):
        a, b, c = three_users
        from ting_ting.models import Block
        tmp_session.add(Block(blocker_id=b.id, blocked_id=a.id))
        tmp_session.commit()
        page, cursor = social.search_users(tmp_session, a.id, query=None, limit=10)
        usernames = [u.username for u in page]
        assert a.username not in usernames
        assert b.username not in usernames
        assert c.username in usernames
        assert cursor is None

    def test_query_filters_by_username_and_display_name(self, tmp_session, three_users):
        a, b, c = three_users
        c.display_name = "Cactus Flower"
        tmp_session.commit()
        by_name, _ = social.search_users(tmp_session, a.id, query="cac", limit=10)
        assert [u.username for u in by_name] == ["unit_c"]
        by_username, _ = social.search_users(tmp_session, a.id, query="UNIT_B", limit=10)
        assert [u.username for u in by_username] == ["unit_b"]

    def test_cursor_walk_visits_every_user_once(self, tmp_session, three_users):
        a, b, c = three_users
        seen = []
        cursor = None
        for _ in range(10):
            page, cursor = social.search_users(
                tmp_session, a.id, query=None, limit=1, cursor=cursor,
            )
            seen.extend(page)
            if cursor is None:
                break
        assert [u.username for u in seen] == ["unit_b", "unit_c"]

    def test_malformed_cursor_resets_not_fails(self, tmp_session, three_users):
        a = three_users[0]
        page, cursor = social.search_users(
            tmp_session, a.id, query=None, limit=1, cursor="not-a-cursor",
        )
        assert [u.username for u in page] == ["unit_b"]
        assert cursor is not None
