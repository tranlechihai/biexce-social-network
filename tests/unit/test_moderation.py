"""Unit tests for moderation reports, roles, warnings, bans, and ledger."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from ting_ting import interactions, moderation, posts as posts_service
from ting_ting.auth import hash_password
from ting_ting.models import (
    Follow, FriendRequest, ModerationAction, Post, User, UserWarning,
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

    def test_concurrent_report_conflict_converges_to_winner(self, tmp_db_path):
        """Deterministic real-DB conflict via a begin_nested hook (same
        pattern as the create_like conflict test): the winning report row is
        committed through a separate connection just before the contender's
        savepoint opens, so the contender's initial SELECT sees nothing, its
        real flush hits the actual ux_reports_dedup unique index, and
        ``create_report`` converges to the winner instead of raising.
        """
        from datetime import datetime, timezone
        from unittest import mock

        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker

        from ting_ting.database import _create_test_engine, _init_test_engine

        engine = _create_test_engine(tmp_db_path)
        _init_test_engine(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        with factory() as sess:
            reporter = User(
                username="race_rpt", email="race_rpt@x.com",
                password_hash=hash_password("pw12345678"),
            )
            target = User(
                username="race_tgt", email="race_tgt@x.com",
                password_hash=hash_password("pw12345678"),
            )
            sess.add_all([reporter, target])
            sess.flush()
            post = Post(author_id=target.id, content="raced post",
                        audience="PUBLIC")
            sess.add(post)
            sess.commit()
            reporter_id, target_id, post_id = reporter.id, target.id, post.id

        with factory() as sess:
            original_begin_nested = sess.begin_nested
            conflict_injected = [False]

            def patched_begin_nested():
                if not conflict_injected[0]:
                    with engine.begin() as conn:
                        conn.execute(text(
                            "INSERT INTO reports "
                            "(reporter_id, target_user_id, post_id,"
                            " comment_id, reason, status, created_at) "
                            "VALUES (:rid, :tid, :pid, NULL, 'spam',"
                            " 'pending', :ts)"
                        ), {
                            "rid": reporter_id,
                            "tid": target_id,
                            "pid": post_id,
                            "ts": datetime.now(timezone.utc),
                        })
                    conflict_injected[0] = True
                return original_begin_nested()

            with mock.patch.object(
                sess, "begin_nested", side_effect=patched_begin_nested,
            ):
                with sess.no_autoflush:
                    row = moderation.create_report(
                        sess, reporter, target_id, reason="spam",
                        post_id=post_id,
                    )

            # Converged to the winner's row; exactly one report exists.
            assert row is not None, "recovery did not find the winner row"
            assert row.reporter_id == reporter_id
        with engine.connect() as conn:
            cnt = conn.execute(
                text("SELECT COUNT(*) FROM reports WHERE post_id = :pid"),
                {"pid": post_id},
            ).scalar()
        assert cnt == 1, f"Expected 1 report row, got {cnt}"

        engine.dispose()


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
        action_types = list(tmp_session.scalars(
            select(ModerationAction.action_type).order_by(ModerationAction.id)
        ))
        assert "user_banned" in action_types
        assert "report_resolved" in action_types

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


class TestRolesWarningsAndLedger:
    def test_warning_is_user_visible_and_writes_ledger(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]

        warning = moderation.warn_user(
            tmp_session, mod, target, reason="harassment", note="Stop targeting users.",
        )
        tmp_session.commit()

        assert warning.user_id == target.id
        assert tmp_session.scalar(select(UserWarning).where(
            UserWarning.user_id == target.id,
        )) is not None
        action = tmp_session.scalar(select(ModerationAction).where(
            ModerationAction.action_type == "warning_issued",
        ))
        assert action is not None
        assert action.actor_id == mod.id
        assert action.target_user_id == target.id

    def test_temporary_ban_expires_and_keeps_history(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]
        expires = datetime.now(timezone.utc) + timedelta(hours=2)

        moderation.ban_user(
            tmp_session, mod, target, reason="spam", expires_at=expires,
        )
        tmp_session.commit()

        assert target.ban_reason == "spam"
        assert target.banned_until is not None
        assert moderation.is_user_banned(target, now=expires - timedelta(seconds=1))
        assert not moderation.is_user_banned(target, now=expires)
        action = tmp_session.scalar(select(ModerationAction).where(
            ModerationAction.action_type == "user_banned",
        ))
        assert action is not None
        assert action.reason == "spam"

    def test_moderator_cannot_enforce_against_staff(
        self, tmp_session, four_users,
    ):
        moderator, peer = four_users[3], four_users[2]
        peer.role = "moderator"

        with pytest.raises(ValueError, match="insufficient_role"):
            moderation.ban_user(tmp_session, moderator, peer, reason="other")

    def test_admin_can_change_non_admin_role_and_action_is_audited(
        self, tmp_session, four_users,
    ):
        target, admin = four_users[1], four_users[3]
        admin.role = "admin"

        moderation.change_role(
            tmp_session, admin, target, new_role="moderator", reason="staffing",
        )
        tmp_session.commit()

        assert target.role == "moderator"
        action = tmp_session.scalar(select(ModerationAction).where(
            ModerationAction.action_type == "role_changed",
        ))
        assert action.previous_state == "user"
        assert action.new_state == "moderator"

    def test_ledger_rows_reject_orm_update(self, tmp_session, four_users):
        target, mod = four_users[1], four_users[3]
        moderation.warn_user(tmp_session, mod, target, reason="spam")
        tmp_session.commit()
        action = tmp_session.scalar(select(ModerationAction))

        action.reason = "rewritten"
        with pytest.raises(ValueError, match="moderation_action_immutable"):
            tmp_session.flush()

    def test_ledger_rows_reject_raw_update_and_delete(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]
        moderation.warn_user(tmp_session, mod, target, reason="spam")
        tmp_session.commit()
        action = tmp_session.scalar(select(ModerationAction))

        with pytest.raises(DatabaseError, match="moderation_action_immutable"):
            tmp_session.execute(text(
                "UPDATE moderation_actions SET reason='rewritten' WHERE id=:id"
            ), {"id": action.id})
        tmp_session.rollback()
        with pytest.raises(DatabaseError, match="moderation_action_immutable"):
            tmp_session.execute(text(
                "UPDATE moderation_actions SET target_user_id=NULL WHERE id=:id"
            ), {"id": action.id})
        tmp_session.rollback()
        with pytest.raises(DatabaseError, match="moderation_action_immutable"):
            tmp_session.execute(text(
                "DELETE FROM moderation_actions WHERE id=:id"
            ), {"id": action.id})

    def test_report_target_hierarchy_is_enforced(
        self, tmp_session, four_users, public_post,
    ):
        reporter, target, moderator = four_users[0], four_users[1], four_users[3]
        target.role = "admin"
        report = moderation.create_report(
            tmp_session, reporter, target.id, reason="other", post_id=public_post.id,
        )

        with pytest.raises(ValueError, match="insufficient_role"):
            moderation.resolve_report(tmp_session, moderator, report)

    def test_target_delete_anonymizes_but_preserves_ledger(
        self, tmp_session, four_users,
    ):
        target, mod = four_users[1], four_users[3]
        moderation.warn_user(tmp_session, mod, target, reason="spam")
        tmp_session.commit()
        action_id = tmp_session.scalar(select(ModerationAction.id))

        tmp_session.delete(target)
        tmp_session.commit()

        action = tmp_session.get(ModerationAction, action_id)
        assert action is not None
        assert action.target_user_id is None
