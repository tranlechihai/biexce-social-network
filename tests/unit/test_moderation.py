"""Unit tests for Increment 5 — moderation service (reports + bans)."""

import pytest
from sqlalchemy import select

from ting_ting import interactions, moderation, posts as posts_service
from ting_ting.auth import hash_password
from ting_ting.models import (
    Follow, FriendRequest, Post, User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def four_users(tmp_session):
    users = []
    for username in ("mmod_a", "mmod_b", "mmod_c", "mmod_mod"):
        u = User(
            username=username,
            email=f"{username}@mmod.com",
            password_hash=hash_password("pass1234"),
        )
        tmp_session.add(u)
        users.append(u)
    users[3].is_moderator = True
    tmp_session.commit()
    for u in users:
        tmp_session.refresh(u)
    return users


@pytest.fixture
def public_post(tmp_session, four_users):
    post = Post(author_id=four_users[1].id, content="reported content",
                audience="PUBLIC")
    tmp_session.add(post)
    tmp_session.commit()
    tmp_session.refresh(post)
    return post


# ---------------------------------------------------------------------------
# create_report
# ---------------------------------------------------------------------------

class TestCreateReport:
    def test_creates_pending_report(self, tmp_session, four_users, public_post):
        reporter, target = four_users[0], four_users[1]
        row = moderation.create_report(
            tmp_session, reporter, target.id, reason="spam",
            post_id=public_post.id,
        )
        assert row.status == moderation.PENDING
        assert row.reporter_id == reporter.id
        assert row.target_user_id == target.id

    def test_idempotent_same_target(self, tmp_session, four_users, public_post):
        reporter, target = four_users[0], four_users[1]
        first = moderation.create_report(
            tmp_session, reporter, target.id, reason="spam",
            post_id=public_post.id,
        )
        again = moderation.create_report(
            tmp_session, reporter, target.id, reason="spam",
            post_id=public_post.id,
        )
        assert first.id == again.id

    def test_different_reporter_gets_own_row(self, tmp_session, four_users,
                                             public_post):
        target = four_users[1]
        first = moderation.create_report(
            tmp_session, four_users[0], target.id, reason="spam",
            post_id=public_post.id,
        )
        second = moderation.create_report(
            tmp_session, four_users[2], target.id, reason="spam",
            post_id=public_post.id,
        )
        assert first.id != second.id

    def test_self_report_rejected(self, tmp_session, four_users):
        with pytest.raises(ValueError, match="self_report"):
            moderation.create_report(
                tmp_session, four_users[0], four_users[0].id, reason="spam",
            )

    def test_invalid_reason_rejected(self, tmp_session, four_users):
        with pytest.raises(ValueError, match="invalid_reason"):
            moderation.create_report(
                tmp_session, four_users[0], four_users[1].id,
                reason="not a reason",
            )

    def test_comment_report_requires_post(
        self, tmp_session, four_users, public_post,
    ):
        comment = interactions.create_comment(
            tmp_session, public_post, four_users[0].id, "a comment",
        )
        with pytest.raises(ValueError, match="content_requires_post"):
            moderation.create_report(
                tmp_session, four_users[2], four_users[1].id,
                reason="spam", comment_id=comment.id, post_id=None,
            )


# ---------------------------------------------------------------------------
# resolve_report
# ---------------------------------------------------------------------------

class TestResolveReport:
    def _pending(self, tmp_session, four_users, public_post):
        row = moderation.create_report(
            tmp_session, four_users[0], four_users[1].id, reason="harassment",
            post_id=public_post.id,
        )
        return row, four_users[3]

    def test_resolve_records_actor_and_note(self, tmp_session, four_users,
                                            public_post):
        row, mod = self._pending(tmp_session, four_users, public_post)
        moderation.resolve_report(tmp_session, mod, row, note="removed content")
        assert row.status == moderation.RESOLVED
        assert row.resolved_by == mod.id
        assert row.resolution_note == "removed content"
        assert row.resolved_at is not None

    def test_dismiss_marks_dismissed(self, tmp_session, four_users, public_post):
        row, mod = self._pending(tmp_session, four_users, public_post)
        moderation.resolve_report(tmp_session, mod, row, dismiss=True)
        assert row.status == moderation.DISMISSED
        assert row.resolved_by == mod.id

    def test_resolving_twice_rejected(self, tmp_session, four_users,
                                      public_post):
        row, mod = self._pending(tmp_session, four_users, public_post)
        moderation.resolve_report(tmp_session, mod, row)
        with pytest.raises(ValueError, match="not_pending"):
            moderation.resolve_report(tmp_session, mod, row)


# ---------------------------------------------------------------------------
# ban / unban
# ---------------------------------------------------------------------------

class TestBan:
    def test_ban_sets_timestamp_and_severs_follows(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]
        tmp_session.add(Follow(follower_id=target.id, followed_id=four_users[0].id))
        tmp_session.add(
            Follow(follower_id=four_users[2].id, followed_id=target.id),
        )
        tmp_session.commit()

        moderation.ban_user(tmp_session, mod, target)
        tmp_session.commit()

        assert target.banned_at is not None
        assert list(tmp_session.scalars(select(Follow))) == []

    def test_ban_severs_friend_requests(self, tmp_session, four_users):
        a, target, mod = four_users[0], four_users[1], four_users[3]
        from ting_ting import social as social_logic
        req = social_logic.create_friend_request(tmp_session, a, target)
        assert req.state == "pending"
        moderation.ban_user(tmp_session, mod, target)
        tmp_session.commit()
        assert list(tmp_session.scalars(select(FriendRequest))) == []

    def test_ban_resolves_pending_reports(self, tmp_session, four_users,
                                          public_post):
        target, mod = four_users[1], four_users[3]
        row = moderation.create_report(
            tmp_session, four_users[0], target.id, reason="spam",
            post_id=public_post.id,
        )
        # An already-resolved report must stay as-is (not touched by ban).
        other_row = moderation.create_report(
            tmp_session, four_users[0], target.id, reason="other",
        )
        moderation.resolve_report(tmp_session, mod, other_row)

        moderation.ban_user(tmp_session, mod, target)
        tmp_session.commit()
        tmp_session.refresh(row)
        assert row.status == moderation.RESOLVED
        assert row.resolved_by == mod.id
        tmp_session.refresh(other_row)
        assert other_row.status == moderation.RESOLVED

    def test_ban_idempotent(self, tmp_session, four_users):
        target, mod = four_users[1], four_users[3]
        moderation.ban_user(tmp_session, mod, target)
        first = target.banned_at
        moderation.ban_user(tmp_session, mod, target)
        tmp_session.commit()
        assert target.banned_at == first

    def test_self_ban_rejected(self, tmp_session, four_users):
        mod = four_users[3]
        with pytest.raises(ValueError, match="self_ban"):
            moderation.ban_user(tmp_session, mod, mod)

    def test_unban_clears_timestamp_not_relationships(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]
        a = four_users[0]
        tmp_session.add(Follow(follower_id=a.id, followed_id=target.id))
        tmp_session.commit()
        moderation.ban_user(tmp_session, mod, target)
        tmp_session.commit()
        moderation.unban_user(tmp_session, mod, target)
        tmp_session.commit()
        assert target.banned_at is None
        # The severed follow is NOT restored by an unban.
        assert list(tmp_session.scalars(select(Follow))) == []

    def test_unban_idempotent(self, tmp_session, four_users):
        target, mod = four_users[1], four_users[3]
        moderation.unban_user(tmp_session, mod, target)
        assert target.banned_at is None

    def test_self_unban_rejected(self, tmp_session, four_users):
        mod = four_users[3]
        with pytest.raises(ValueError, match="self_unban"):
            moderation.unban_user(tmp_session, mod, mod)

    def test_banned_posts_leave_feeds(self, tmp_session, four_users):
        target, other = four_users[1], four_users[2]
        post = Post(author_id=target.id, content="banned post", audience="PUBLIC")
        tmp_session.add(post)
        tmp_session.commit()
        moderation.ban_user(tmp_session, four_users[3], target)
        tmp_session.commit()
        page, _ = posts_service.query_feed(tmp_session, other.id, limit=10)
        assert post.id not in [p.id for p in page]

    def test_mute_post_does_not_create_user_mute(self, tmp_session, four_users,
                                                  public_post):
        from ting_ting import social as social_logic
        a, target = four_users[0], four_users[1]
        social_logic.mute_post(tmp_session, a, public_post.id)
        assert social_logic.is_muted_by(tmp_session, a.id, target.id) is False
