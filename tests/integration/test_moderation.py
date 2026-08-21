"""Integration tests for Increment 5 — Safety & Moderation APIs.

Covers: report filing (post/comment/account, visibility, dedup), the
moderator report queue (list/resolve/dismiss + 403 for non-moderators),
ban/unban enforcement (login 403, API 401, feed removal, discovery 404),
and moderator content removal.
"""

import pytest
from fastapi import status


class _M:
    PREFIX = "i5"

    @classmethod
    def register(cls, client, username):
        name = f"{cls.PREFIX}_{username}"
        resp = client.post("/api/auth/register", json={
            "username": name,
            "email": f"{name}@i5.com",
            "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        return resp.json()["id"]

    @classmethod
    def login(cls, client, username, password="securepass1"):
        resp = client.post("/api/auth/login", json={
            "identifier": f"{cls.PREFIX}_{username}",
            "password": password,
        })
        assert resp.status_code == status.HTTP_200_OK, resp.text

    @classmethod
    def create_post(cls, client, content, audience="PUBLIC"):
        resp = client.post("/api/posts", json={"content": content, "audience": audience})
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        return resp.json()

    @classmethod
    def make_moderator(cls, db_session, user_id):
        from ting_ting.models import User
        user = db_session.get(User, user_id)
        user.is_moderator = True
        db_session.commit()
        return user


@pytest.fixture
def db_session(tmp_engine):
    from sqlalchemy.orm import sessionmaker
    session = sessionmaker(bind=tmp_engine, expire_on_commit=False)()
    yield session
    session.close()


def _ban(client, user_id):
    return client.post("/api/social/bans", json={"user_id": user_id})


# ---------------------------------------------------------------------------
# Report filing
# ---------------------------------------------------------------------------

class TestReportFiling:

    def test_report_a_public_post(self, client):
        _a, b = _M.register(client, "rpt_a"), _M.register(client, "rpt_b")
        _M.login(client, "rpt_b")
        post = _M.create_post(client, "spammy")
        _M.login(client, "rpt_a")
        resp = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        })
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["target_user"]["id"] == b

    def test_report_dedup_same_target(self, client):
        _a, b = _M.register(client, "dup_a"), _M.register(client, "dup_b")
        _M.login(client, "dup_b")
        post = _M.create_post(client, "spammy")
        _M.login(client, "dup_a")
        first = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        })
        second = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        })
        assert second.status_code == status.HTTP_201_CREATED
        assert first.json()["id"] == second.json()["id"]

    def test_report_post_must_target_post_author(self, client):
        """A post report's target is pinned to the post's author; pointing it
        at an unrelated account is rejected (client-provided targets are not
        trusted for content reports)."""
        _a = _M.register(client, "pa_a")
        b = _M.register(client, "pa_b")
        c = _M.register(client, "pa_c")
        _M.login(client, "pa_b")
        post = _M.create_post(client, "authored by b")
        _M.login(client, "pa_a")
        wrong = client.post("/api/reports", json={
            "target_user_id": c, "post_id": post["id"], "reason": "spam",
        })
        assert wrong.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert wrong.json()["error"]["code"] == "validation"

        right = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        })
        assert right.status_code == status.HTTP_201_CREATED, right.text

    def test_post_delete_not_blocked_by_bare_report(self, client, db_session):
        """Deleting a post sets the anchored report's post_id to NULL via the
        FK; that must not violate ux_reports_dedup even when the same
        reporter also holds a bare account report on the same target (the
        raw-NULL post_id keeps the two rows apart — a fully COALESCEd index
        would collide them and wedge the deletion forever)."""
        _a = _M.register(client, "wedge_a")
        b = _M.register(client, "wedge_b")
        mod = _M.register(client, "wedge_mod")
        _M.make_moderator(db_session, mod)
        _M.login(client, "wedge_b")
        post = _M.create_post(client, "will be removed")
        _M.login(client, "wedge_a")
        assert client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        }).status_code == 201
        assert client.post("/api/reports", json={
            "target_user_id": b, "reason": "other",
        }).status_code == 201
        _M.login(client, "wedge_mod")
        resp = client.delete(f"/api/mod/posts/{post['id']}")
        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_report_invisible_post_404(self, client):
        _a, b = _M.register(client, "inv_a"), _M.register(client, "inv_b")
        _M.login(client, "inv_b")
        post = _M.create_post(client, "secret", audience="ONLY_ME")
        _M.login(client, "inv_a")
        # A post the reporter cannot see cannot be reported (404, no leak).
        resp = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"], "reason": "spam",
        })
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_self_report_rejected(self, client):
        _a, b = _M.register(client, "self_a"), _M.register(client, "self_b")
        _M.login(client, "self_b")
        _M.create_post(client, "x")
        resp = client.post("/api/reports", json={
            "target_user_id": b, "reason": "other",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.json()["error"]["code"] == "validation"

    def test_invalid_reason_rejected(self, client):
        _a, b = _M.register(client, "rsn_a"), _M.register(client, "rsn_b")
        _M.login(client, "rsn_b")
        _M.create_post(client, "x")
        _M.login(client, "rsn_a")
        resp = client.post("/api/reports", json={
            "target_user_id": b, "reason": "because",
        })
        # Pydantic Literal rejects unknown reasons at validation (422).
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_report_a_comment(self, client):
        _a, b = _M.register(client, "cmt_a"), _M.register(client, "cmt_b")
        _M.login(client, "cmt_b")
        post = _M.create_post(client, "thread")
        comment = client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "rude comment"},
        )
        assert comment.status_code == 201
        _M.login(client, "cmt_a")
        resp = client.post("/api/reports", json={
            "target_user_id": b, "post_id": post["id"],
            "comment_id": comment.json()["id"], "reason": "harassment",
        })
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()
        assert body["comment_id"] is not None
        # The report is anchored to the comment's own post.
        assert body["post_id"] == post["id"]


# ---------------------------------------------------------------------------
# Moderator queue
# ---------------------------------------------------------------------------

class TestModeratorQueue:

    def _seed(self, client):
        _reporter, target, _mod = (
            _M.register(client, "q_rpt"), _M.register(client, "q_tgt"),
            _M.register(client, "q_mod"),
        )
        _M.login(client, "q_tgt")
        post = _M.create_post(client, "bad post")
        _M.login(client, "q_rpt")
        report = client.post("/api/reports", json={
            "target_user_id": target, "post_id": post["id"], "reason": "spam",
        })
        assert report.status_code == 201
        _M.login(client, "q_mod")
        return target, post, report.json()["id"]

    def test_list_reports_requires_moderator(self, client):
        self._seed(client)
        client.post("/api/auth/logout")
        _M.login(client, "q_rpt")  # a normal user
        resp = client.get("/api/reports")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_list_reports_and_resolve(self, client, db_session):
        target, post, report_id = self._seed(client)
        _M.make_moderator(db_session, _mod_id(client, "q_mod"))

        resp = client.get("/api/reports")
        assert resp.status_code == status.HTTP_200_OK
        items = resp.json()
        assert any(r["id"] == report_id for r in items)
        assert any(r["status"] == "pending" for r in items)

        # Resolve with an audit note.
        resolved = client.post(
            f"/api/reports/{report_id}/resolve", params={"note": "removed post"},
        )
        assert resolved.status_code == status.HTTP_200_OK, resolved.text
        body = resolved.json()
        assert body["status"] == "resolved"
        assert body["resolution_note"] == "removed post"
        assert body["resolved_by"] is not None

        # Resolving twice is a conflict.
        again = client.post(f"/api/reports/{report_id}/resolve")
        assert again.status_code == status.HTTP_409_CONFLICT

    def test_dismiss_flow(self, client, db_session):
        target, post, report_id = self._seed(client)
        _M.make_moderator(db_session, _mod_id(client, "q_mod"))

        dismissed = client.post(f"/api/reports/{report_id}/dismiss")
        assert dismissed.status_code == status.HTTP_200_OK, dismissed.text
        assert dismissed.json()["status"] == "dismissed"

    def test_status_filter(self, client, db_session):
        target, post, report_id = self._seed(client)
        _M.make_moderator(db_session, _mod_id(client, "q_mod"))

        client.post(f"/api/reports/{report_id}/dismiss")
        pending = client.get("/api/reports", params={"status": "pending"}).json()
        assert all(r["status"] == "pending" for r in pending)
        dismissed = client.get("/api/reports", params={"status": "dismissed"}).json()
        assert any(r["id"] == report_id for r in dismissed)


def _mod_id(client, username):
    """Look up the moderator user id by username from the client's DB."""
    from ting_ting import database as db_mod
    from ting_ting.models import User
    from sqlalchemy import select
    session = db_mod.get_session_factory()()
    try:
        u = session.scalar(select(User).where(User.username == f"{_M.PREFIX}_{username}"))
        return u.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Ban / unban enforcement
# ---------------------------------------------------------------------------

class TestBan:

    def _setup(self, client, db_session, tag):
        mod = _M.register(client, f"{tag}_mod")
        target = _M.register(client, f"{tag}_tgt")
        _M.make_moderator(db_session, mod)
        return mod, target

    def test_ban_requires_moderator(self, client, db_session):
        _, target = self._setup(client, db_session, "nm")
        client.post("/api/auth/logout")
        _M.login(client, "nm_tgt")  # normal user tries to ban
        resp = _ban(client, target)
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_ban_and_unban_cycle(self, client, db_session):
        mod, target = self._setup(client, db_session, "ban")
        _M.login(client, "ban_mod")

        resp = _ban(client, target)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # A banned user cannot log in.
        resp = client.post("/api/auth/login", json={
            "identifier": f"{_M.PREFIX}_ban_tgt", "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error"]["code"] == "banned"

        # Unban restores login.
        _M.login(client, "ban_mod")
        unbanned = client.delete(f"/api/social/bans/{target}")
        assert unbanned.status_code == status.HTTP_200_OK, unbanned.text
        resp = client.post("/api/auth/login", json={
            "identifier": f"{_M.PREFIX}_ban_tgt", "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_200_OK

    def test_banned_profile_hidden_from_discovery(self, client, db_session):
        mod, target = self._setup(client, db_session, "disc")
        _M.login(client, "disc_mod")
        assert _ban(client, target).status_code == 200
        # Public profile 404s while banned.
        resp = client.get(f"/api/users/{_M.PREFIX}_disc_tgt")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        # Search no longer returns the banned user.
        results = client.get("/api/users", params={"q": "disc"}).json()
        assert all(u["username"] != f"{_M.PREFIX}_disc_tgt" for u in results)

    def test_banned_posts_leave_feed(self, client, db_session):
        mod, target = self._setup(client, db_session, "feed")
        _M.login(client, "feed_tgt")
        _M.create_post(client, "banned post")
        _M.login(client, "feed_mod")
        assert _ban(client, target).status_code == 200
        # A different viewer's feed no longer shows the banned author's post.
        _bystander = _M.register(client, "feed_by")
        _M.login(client, "feed_by")
        feed = client.get("/api/feed").json()
        assert all(p["author_id"] != target for p in feed)

    def test_banned_posts_hidden_from_direct_read(self, client, db_session):
        """Feed suppression must not be bypassable via a direct post id: a
        banned author's posts 404 for third parties (and their comment
        threads too), not just disappear from feeds."""
        mod, target = self._setup(client, db_session, "dread")
        _M.login(client, "dread_tgt")
        post = _M.create_post(client, "still here?")
        _M.login(client, "dread_mod")
        assert _ban(client, target).status_code == 200

        _fan = _M.register(client, "dread_fan")
        _M.login(client, "dread_fan")
        assert client.get(f"/api/posts/{post['id']}").status_code == 404
        assert client.get(
            f"/api/posts/{post['id']}/comments"
        ).status_code == 404

    def test_unban_idempotent(self, client, db_session):
        mod, target = self._setup(client, db_session, "uidem")
        _M.login(client, "uidem_mod")
        assert _ban(client, target).status_code == 200
        assert client.delete(f"/api/social/bans/{target}").status_code == 200
        # Second unban is still 200 (no-op), not an error.
        assert client.delete(f"/api/social/bans/{target}").status_code == 200


# ---------------------------------------------------------------------------
# Moderator content removal
# ---------------------------------------------------------------------------

class TestModContentRemoval:

    def test_mod_can_delete_another_users_post(self, client, db_session):
        mod = _M.register(client, "mcr_mod")
        _owner = _M.register(client, "mcr_own")
        _M.make_moderator(db_session, mod)
        _M.login(client, "mcr_own")
        post = _M.create_post(client, "to be removed")
        _M.login(client, "mcr_mod")
        resp = client.delete(f"/api/mod/posts/{post['id']}")
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # The post is gone for the owner too.
        _M.login(client, "mcr_own")
        assert client.get(f"/api/posts/{post['id']}").status_code == 404

    def test_non_moderator_cannot_use_mod_delete(self, client, db_session):
        _owner = _M.register(client, "nmc_own")
        _victim = _M.register(client, "nmc_vis")
        _M.login(client, "nmc_own")
        post = _M.create_post(client, "safe post")
        _M.login(client, "nmc_vis")  # normal user
        resp = client.delete(f"/api/mod/posts/{post['id']}")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_mod_can_delete_a_comment(self, client, db_session):
        mod = _M.register(client, "mcc_mod")
        _owner = _M.register(client, "mcc_own")
        _commenter = _M.register(client, "mcc_cmt")
        _M.make_moderator(db_session, mod)
        _M.login(client, "mcc_own")
        post = _M.create_post(client, "thread")
        _M.login(client, "mcc_cmt")
        c = client.post(f"/api/posts/{post['id']}/comments", json={"content": "bad"})
        comment_id = c.json()["id"]
        _M.login(client, "mcc_mod")
        resp = client.delete(f"/api/mod/comments/{comment_id}")
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # Comment list no longer contains it.
        _M.login(client, "mcc_own")
        comments = client.get(f"/api/posts/{post['id']}/comments").json()
        assert all(cmt["id"] != comment_id for cmt in comments)


# ---------------------------------------------------------------------------
# Banned user API auth (401)
# ---------------------------------------------------------------------------

class TestBannedAuth:

    def test_banned_token_rejected_on_api(self, client, db_session):
        mod = _M.register(client, "bauth_mod")
        target = _M.register(client, "bauth_tgt")
        _M.make_moderator(db_session, mod)
        # Mint a valid token for the target before the ban.
        _M.login(client, "bauth_tgt")
        _M.login(client, "bauth_mod")
        assert _ban(client, target).status_code == 200

        # Re-login is refused while banned.
        resp = client.post("/api/auth/login", json={
            "identifier": f"{_M.PREFIX}_bauth_tgt", "password": "securepass1",
        })
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error"]["code"] == "banned"
