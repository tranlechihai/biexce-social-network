"""Missing-scope black-box tests identified by t-006 coverage audit.

These tests fill gaps found between the MVP scope (TING_TING_MVP_SCOPE.md)
and existing test coverage:

GAP1: TT-FEED — 3-user non-friend feed isolation
  Scope §4.3: "Carol's FRIENDS post does NOT appear in Alice or Bob's feed (not friends)."
  No existing integration test creates a 3-user setup where one user is isolated
  and verifies their posts don't leak into friends' feeds.

GAP2: TT-WEB — Reject friend request via web form
  Browser smoke checklist: "Reject pending request — status changes to 'none'."
  Web route POST /social/reject-request exists but has no dedicated web test.

GAP3: TT-WEB — Unblock via web form
  Browser smoke checklist: "Unblock user — no relationship restored (status 'none')."
  Web route POST /social/unblock exists but has no dedicated web test.

GAP4: TT-WEB — Feed accessibility after cookie logout
  Browser smoke checklist: "After logout, accessing /feed — redirects to login."
  Existing web tests verify logout clears the cookie but don't verify
  that /feed properly denies unauthenticated access via the web layer.

GAP5: TT-WEB — Empty post content shows error message in HTML
  Browser smoke checklist: "Post form: content is required — empty content shows error."
  Existing test checks status code redirect but not that the error is visible.

GAP6: TT-CLI — Fresh CLI init→seed end-to-end
  ``python -m ting_ting`` (no args) must actually create all MVP tables on a
  fresh DB. Followed by ``python -m ting_ting seed`` the seeded data must be
  queryable. Existing ``test_seed.py`` tests ``run()`` in-process but never
  exercises the CLI entry point (`__main__.py`).
"""

from fastapi import status


class _WebHelpers:
    @staticmethod
    def register(client, username, email, password="securepass1"):
        return client.post("/web/register", data={
            "username": username, "email": email, "password": password,
        }, follow_redirects=False)

    @staticmethod
    def login(client, identifier, password="securepass1"):
        return client.post("/web/login", data={
            "identifier": identifier, "password": password,
        }, follow_redirects=False)

    @staticmethod
    def logout(client):
        return client.post("/web/logout", follow_redirects=False)


# ---------------------------------------------------------------------------
# GAP1: TT-FEED — 3-user non-friend feed isolation (AC3)
# ---------------------------------------------------------------------------

class TestGap3UserFeedIsolation:

    def test_carol_friends_post_not_in_alice_feed(self, client):
        """FRIENDS posts from a user who is NOT friends with the viewer
        must not appear in that viewer's feed — even if viewer has other friends."""

        # Create alice, bob, carol (all separate registrations)
        _WebHelpers.register(client, "gap_carol", "gcarol@tt.com")

        # Register pair alice+bob and make them friends
        a_resp = client.post("/api/auth/register", json={
            "username": "gap_alice3", "email": "galice3@tt.com", "password": "securepass1"
        })
        assert a_resp.status_code == status.HTTP_201_CREATED
        _a_id = a_resp.json()["id"]

        b_resp = client.post("/api/auth/register", json={
            "username": "gap_bob3", "email": "gbob3@tt.com", "password": "securepass1"
        })
        assert b_resp.status_code == status.HTTP_201_CREATED
        b_id = b_resp.json()["id"]

        # Alice sends friend request, bob accepts
        client.post("/api/auth/login", json={"identifier": "gap_alice3", "password": "securepass1"})
        req = client.post("/api/social/requests", json={"target_user_id": b_id})
        req_id = req.json()["id"]

        client.post("/api/auth/login", json={"identifier": "gap_bob3", "password": "securepass1"})
        client.post("/api/social/requests/accept", json={"request_id": req_id})

        # Carol creates a FRIENDS post (not friends with alice or bob)
        _WebHelpers.login(client, "gap_carol")
        carol_post_resp = client.post("/api/posts", json={
            "content": "Carol's FRIENDS post that should not appear",
            "audience": "FRIENDS"
        })
        assert carol_post_resp.status_code == status.HTTP_201_CREATED
        carol_post_id = carol_post_resp.json()["id"]

        # Alice views feed — Carol's post must NOT appear
        client.post("/api/auth/login", json={"identifier": "gap_alice3", "password": "securepass1"})
        feed = client.get("/api/feed").json()
        feed_ids = [p["id"] for p in feed]
        assert carol_post_id not in feed_ids, \
            f"Carol's post {carol_post_id} leaked into Alice's feed (friends-only isolation failed)"

        # Bob views feed — Carol's post must NOT appear
        client.post("/api/auth/login", json={"identifier": "gap_bob3", "password": "securepass1"})
        feed = client.get("/api/feed").json()
        feed_ids = [p["id"] for p in feed]
        assert carol_post_id not in feed_ids, \
            f"Carol's post {carol_post_id} leaked into Bob's feed (friends-only isolation failed)"

    def test_carol_post_visible_in_own_feed(self, client):
        """Carol must see her own FRIENDS post in her feed."""
        # (Setup is same, just verify Carol can see her own post)
        _WebHelpers.register(client, "gap_carol2", "gcarol2@tt.com")
        _WebHelpers.login(client, "gap_carol2")
        carol_post_resp = client.post("/api/posts", json={
            "content": "Carol's own post",
            "audience": "FRIENDS"
        })
        assert carol_post_resp.status_code == status.HTTP_201_CREATED
        carol_post_id = carol_post_resp.json()["id"]

        feed = client.get("/api/feed").json()
        feed_ids = [p["id"] for p in feed]
        assert carol_post_id in feed_ids, \
            "Carol's OWN FRIENDS post should appear in her own feed"


# ---------------------------------------------------------------------------
# GAP2: TT-WEB — Reject friend request via web form (AC5)
# ---------------------------------------------------------------------------

class TestGapRejectRequestWeb:

    def test_reject_friend_request_via_web_form(self, client):
        """Sender sends friend request, recipient rejects via web form."""
        # Register sender (A) and recipient (B)
        a = client.post("/api/auth/register", json={
            "username": "gap_rj_a", "email": "grja@tt.com", "password": "securepass1"
        })
        assert a.status_code == status.HTTP_201_CREATED
        a_data = a.json()

        b = client.post("/api/auth/register", json={
            "username": "gap_rj_b", "email": "grjb@tt.com", "password": "securepass1"
        })
        assert b.status_code == status.HTTP_201_CREATED
        b_data = b.json()

        # A sends friend request to B via API
        _WebHelpers.login(client, "gap_rj_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": b_data["id"]})
        req_id = req_resp.json()["id"]

        # B rejects via web form
        _WebHelpers.login(client, "gap_rj_b")
        resp = client.post("/web/social/reject-request", data={
            "request_id": req_id,
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK

        # Verify state is not "friends" — should be "none"
        rel = client.get(f"/api/social/relationship/{a_data['id']}").json()
        assert rel["state"] == "none", f"Expected 'none' after web reject, got {rel['state']}"


# ---------------------------------------------------------------------------
# GAP3: TT-WEB — Unblock via web form (AC5)
# ---------------------------------------------------------------------------

class TestGapUnblockWeb:

    def test_unblock_via_web_form_restores_nothing(self, client):
        """Block then unblock via web — no friendship restored."""
        a = client.post("/api/auth/register", json={
            "username": "gap_ub_a", "email": "guba@tt.com", "password": "securepass1"
        })
        assert a.status_code == status.HTTP_201_CREATED
        a_data = a.json()

        b = client.post("/api/auth/register", json={
            "username": "gap_ub_b", "email": "gubb@tt.com", "password": "securepass1"
        })
        assert b.status_code == status.HTTP_201_CREATED
        b_data = b.json()

        # Make them friends first
        _WebHelpers.login(client, "gap_ub_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": b_data["id"]})
        req_id = req_resp.json()["id"]

        _WebHelpers.login(client, "gap_ub_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert client.get(f"/api/social/relationship/{a_data['id']}").json()["state"] == "friends"

        # A blocks B
        _WebHelpers.login(client, "gap_ub_a")
        resp = client.post("/web/social/block", data={
            "target_username": "gap_ub_b",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert client.get(f"/api/social/relationship/{b_data['id']}").json()["state"] == "blocked_by_me"

        # A unblocks B via web
        resp = client.post("/web/social/unblock", data={
            "target_username": "gap_ub_b",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK

        # Status should be "none" — friendship NOT restored
        rel = client.get(f"/api/social/relationship/{b_data['id']}").json()
        assert rel["state"] == "none", f"Expected 'none' after unblock, got {rel['state']}"


# ---------------------------------------------------------------------------
# GAP4: TT-WEB — Feed access denied after cookie logout (AC5)
# ---------------------------------------------------------------------------

class TestGapFeedAfterLogout:

    def test_feed_redirects_to_login_after_cookie_logout(self, client):
        """After web cookie logout, accessing /feed redirects to login page.

        Web routes use get_current_user_web which returns a 302 redirect to
        /web/login when unauthenticated. The login page then renders as 200
        HTML. Feed content is NOT shown. API routes still return JSON 401.
        """
        _WebHelpers.register(client, "gap_logout2", "glogout2@tt.com")
        # Verify feed is accessible before logout
        resp = client.get("/web/feed", follow_redirects=True)
        # Should get the feed page (HTML)
        assert resp.status_code == status.HTTP_200_OK
        assert "<title>" in resp.text  # Some HTML content

        # Logout
        _WebHelpers.logout(client)

        # Access feed AFTER logout — should NOT serve feed content.
        # The web route redirects to /web/login (302), which TestClient
        # follows → login page at 200.
        resp = client.get("/web/feed", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify it's the login page, not the feed page
        assert "Đăng nhập" in resp.text
        assert "Your Feed" not in resp.text

    def test_feed_api_returns_401_after_cookie_logout(self, client):
        """API feed also denied after cookie session is cleared by logout."""
        _WebHelpers.register(client, "gap_logout3", "glogout3@tt.com")

        # After register, cookie is set. Verify API works.
        resp = client.get("/api/feed")
        assert resp.status_code == status.HTTP_200_OK

        # Logout via web (clears cookie)
        _WebHelpers.logout(client)

        # API should now return 401
        resp = client.get("/api/feed")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GAP5: TT-WEB — Empty content error visible in HTML (AC5)
# ---------------------------------------------------------------------------

class TestGapEmptyContentErrorVisible:

    def test_empty_post_content_shows_error_in_html(self, client):
        """Empty post content submission renders error message in feed page HTML."""
        _WebHelpers.register(client, "gap_empty", "gempty@tt.com")

        # Submit empty content post
        resp = client.post("/web/posts/create", data={
            "content": "",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK

        html = resp.text
        # The web route redirects to /feed?error=empty
        # The feed template should show an error message
        html_lower = html.lower()
        assert "error" in html_lower or "empty" in html_lower, \
            f"Empty post content error should be visible in feed HTML. " \
            f"URL params visible: {'error=empty' in html_lower}"

    def test_empty_comment_content_shows_redirect(self, client):
        """Empty comment submission redirects with error indicator."""
        _WebHelpers.register(client, "gap_ecmt", "gdecmt@tt.com")

        # Create a post first
        client.post("/web/posts/create", data={
            "content": "Valid post content",
            "audience": "ONLY_ME",
        }, follow_redirects=True)

        # Get the post ID from feed
        resp = client.get("/web/feed")
        assert resp.status_code == status.HTTP_200_OK
        import re
        match = re.search(r'action="/web/posts/(\d+)/comment"', resp.text)
        assert match, "Could not find comment form in rendered feed"
        post_id = match.group(1)

        # Submit empty comment
        resp = client.post(f"/web/posts/{post_id}/comment", data={
            "content": "",
        }, follow_redirects=False)
        # Should redirect (303) with error parameter
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=empty_comment" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# GAP6: TT-CLI — Black-box CLI init→seed (AC6)
# ---------------------------------------------------------------------------

class TestGapCLIInitAndSeed:

    def test_cli_init_creates_tables_then_seed_populates(self, tmp_path):
        """End-to-end: ``python -m ting_ting`` creates schema, then
        ``python -m ting_ting seed`` populates data. Uses subprocess so
        the actual __main__.py entry point is exercised."""
        import os
        import subprocess
        import sys

        db_path = str(tmp_path / "cli_test.db")
        jwt_secret = "cli_test_jwt_secret_do_not_use"
        demo_pw = "CLISeedPass1"

        env = os.environ.copy()
        env["TING_DATABASE_URL"] = f"sqlite:///{db_path}"
        env["TING_JWT_SECRET"] = jwt_secret
        env["TING_DEMO_PASSWORD"] = demo_pw
        env["TING_COOKIE_SECURE"] = "false"

        # Phase 1: CLI init — creates all tables
        init_result = subprocess.run(
            [sys.executable, "-m", "ting_ting"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init_result.returncode == 0, \
            f"CLI init failed (exit {init_result.returncode}):\n{init_result.stderr}"
        assert "Schema validation & initialization passed" in init_result.stdout

        # Verify tables were actually created by querying the DB directly
        from sqlalchemy import create_engine, inspect
        engine = create_engine(f"sqlite:///{db_path}")
        existing = set(inspect(engine).get_table_names())
        engine.dispose()
        for table in ["users", "friend_requests", "blocks", "posts", "likes", "comments"]:
            assert table in existing, f"CLI init did not create table '{table}'"

        # Phase 2: CLI seed — populates data
        seed_result = subprocess.run(
            [sys.executable, "-m", "ting_ting", "seed"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert seed_result.returncode == 0, \
            f"CLI seed failed (exit {seed_result.returncode}):\n{seed_result.stderr}"
        assert "Seed completed successfully" in seed_result.stdout
        assert "alice" in seed_result.stdout
        assert "bob" in seed_result.stdout

        # Phase 3: Verify data is queryable from a new engine
        from sqlalchemy import text
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                user_count = conn.execute(
                    text("SELECT COUNT(*) FROM users")
                ).scalar()
        finally:
            engine.dispose()
        assert user_count == 3, f"Expected 3 users after seed, got {user_count}"

        # Phase 4: Verify double-seed is refused
        double_seed = subprocess.run(
            [sys.executable, "-m", "ting_ting", "seed"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Seed must refuse — either exit code 1 or refusal message
        refused = (
            double_seed.returncode != 0
            or "already contains data" in double_seed.stderr
        )
        assert refused, "Double seed was not refused (should exit nonzero or error)"
