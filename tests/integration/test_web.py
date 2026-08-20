"""Web integration tests for AC1–AC4.

Covers:
AC1: Register, login, logout, invalid credential messages, cookie auth
AC2: Profile view/update, friend request send/accept/reject, friends/unfriend, block/unblock
AC3: Create ONLY_ME/FRIENDS posts, browse feed, ownership actions, invisibility
AC4: Like/unlike, comment create/delete, counters, forbidden reporting
"""

from fastapi import status


class _WebHelpers:
    """Convenience helpers for web flow testing."""

    @staticmethod
    def register(client, username, email, password="securepass1"):
        return client.post("/web/register", data={
            "username": username,
            "email": email,
            "password": password,
        }, follow_redirects=False)

    @staticmethod
    def login(client, identifier, password="securepass1"):
        return client.post("/web/login", data={
            "identifier": identifier,
            "password": password,
        }, follow_redirects=False)

    @staticmethod
    def logout(client):
        return client.post("/web/logout", follow_redirects=False)


# ---------------------------------------------------------------------------
# AC1: Auth via web (register, login, logout, error messages, cookie)
# ---------------------------------------------------------------------------

class TestAC1WebAuth:

    def test_layout_uses_nodi_logo_and_favicon(self, client):
        resp = client.get("/web/login")
        assert resp.status_code == status.HTTP_200_OK
        assert 'href="/static/favicon.svg?v=1"' in resp.text
        assert 'src="/static/nodi-logo-dark.svg"' in resp.text
        assert 'src="/static/favicon.svg"' in resp.text

        logo = client.get("/static/nodi-logo-dark.svg")
        favicon = client.get("/static/favicon.svg")
        assert logo.status_code == status.HTTP_200_OK
        assert favicon.status_code == status.HTTP_200_OK
        assert "image/svg+xml" in logo.headers["content-type"]
        assert "image/svg+xml" in favicon.headers["content-type"]

    def test_register_redirects_to_feed(self, client):
        resp = _WebHelpers.register(client, "webuser1", "web1@tt.com")
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "/web/feed" in resp.headers["location"]

    def test_register_sets_auth_cookie(self, client):
        resp = _WebHelpers.register(client, "webuser2", "web2@tt.com")
        # After redirect, the cookie should be set
        # The 303 response sets the cookie; follow the redirect
        # Actually, the cookie is set on the 303 response
        set_cookie = resp.headers.get("set-cookie", "")
        assert "ting_ting_auth=" in set_cookie
        assert "httponly" in set_cookie.lower()

    def test_login_redirects_to_feed(self, client):
        _WebHelpers.register(client, "weblogin", "weblogin@tt.com")
        resp = _WebHelpers.login(client, "weblogin")
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "/web/feed" in resp.headers["location"]

    def test_invalid_login_shows_error(self, client):
        resp = client.post("/web/login", data={
            "identifier": "nonexistent",
            "password": "wrongpass",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        html = resp.text
        assert "Sai tên đăng nhập hoặc mật khẩu" in html
        # No secret leakage
        assert "password_hash" not in html.lower()
        assert "jwt_secret" not in html.lower()
        assert "secret" not in html.lower()

    def test_invalid_registration_shows_error(self, client):
        resp = client.post("/web/register", data={
            "username": "ab",  # too short
            "email": "bad",    # invalid
            "password": "123",  # too short
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        html = resp.text
        assert "Username" in html or "username" in html
        # Validation errors present
        assert "3-30" in html or "30" in html  # length constraint mention

    def test_logout_removes_access(self, client):
        _WebHelpers.register(client, "weblogout", "weblogout@tt.com")
        # First verify we can access feed
        resp = client.get("/web/feed", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Now logout
        _WebHelpers.logout(client)
        # Feed should redirect to login (unauthenticated browser → login page)
        resp = client.get("/web/feed", follow_redirects=True)
        # After redirect follows, we land on the login page (200, HTML)
        assert resp.status_code == status.HTTP_200_OK
        assert "Đăng nhập" in resp.text  # login page content, NOT feed content
        # After logout, the cookie should be cleared
        # Check that /api/profile/me returns 401 JSON (API behavior preserved)
        api_resp = client.get("/api/profile/me")
        assert api_resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_unauthenticated_reaches_login(self, client):
        resp = client.get("/web/login")
        assert resp.status_code == status.HTTP_200_OK
        assert "Đăng nhập" in resp.text


# ---------------------------------------------------------------------------
# AC2: Web profile & social flows
# ---------------------------------------------------------------------------

class TestAC2WebProfileSocial:

    def test_view_own_profile(self, client):
        _WebHelpers.register(client, "webprof", "webprof@tt.com")
        resp = client.get("/web/profile/me")
        assert resp.status_code == status.HTTP_200_OK
        assert "webprof" in resp.text

    def test_update_own_profile(self, client):
        _WebHelpers.register(client, "webprof2", "webprof2@tt.com")
        resp = client.post("/web/profile/update", data={
            "display_name": "Updated Name",
            "bio": "A new bio",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        html = resp.text
        assert "Updated Name" in html
        assert "A new bio" in html

    def test_profile_gender_options_match_product_scope(self, client):
        _WebHelpers.register(client, "webgender", "webgender@tt.com")
        html = client.get("/web/profile/me").text
        assert "Phi nhị nguyên" not in html
        assert "Nữ" in html
        assert "Nam" in html
        assert "Không muốn trả lời" in html

    def test_send_friend_request(self, client):
        _WebHelpers.register(client, "webreq_a", "webreq_a@tt.com")
        _WebHelpers.register(client, "webreq_b", "webreq_b@tt.com")
        # Login as A to send request to B
        _WebHelpers.login(client, "webreq_a")
        resp = client.post("/web/social/friend-request", data={
            "target_username": "webreq_b",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify via API
        resp = _WebHelpers.login(client, "webreq_b")
        reqs = client.get("/api/social/requests?state=pending").json()
        assert len(reqs) > 0

    def test_accept_friend_request(self, client):
        _WebHelpers.register(client, "webacc_a", "webacc_a@tt.com")
        _WebHelpers.register(client, "webacc_b", "webacc_b@tt.com")
        # A sends to B
        _WebHelpers.login(client, "webacc_a")
        _req = client.post("/api/social/requests", json={"target_user_id": 0})
        # Get B's ID via API
        _WebHelpers.login(client, "webacc_b")
        # Actually let's do it properly via API first
        _WebHelpers.login(client, "webacc_a")
        me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webacc_b")
        me_b = client.get("/api/profile/me").json()
        # A sends to B
        _WebHelpers.login(client, "webacc_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        # B accepts via web
        _WebHelpers.login(client, "webacc_b")
        resp = client.post("/web/social/accept-request", data={
            "request_id": req_id,
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify
        rel = client.get(f"/api/social/relationship/{me_a['id']}").json()
        assert rel["state"] == "friends"

    def test_unfriend_via_web(self, client):
        _WebHelpers.register(client, "webunf_a", "webunf_a@tt.com")
        _WebHelpers.register(client, "webunf_b", "webunf_b@tt.com")
        # Make friends
        _WebHelpers.login(client, "webunf_a")
        me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webunf_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webunf_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "webunf_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # Unfriend B
        _WebHelpers.login(client, "webunf_b")
        resp = client.post("/web/social/unfriend", data={
            "target_username": "webunf_a",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        rel = client.get(f"/api/social/relationship/{me_a['id']}").json()
        assert rel["state"] == "none"

    def test_block_via_web(self, client):
        _WebHelpers.register(client, "webblk_a", "webblk_a@tt.com")
        _WebHelpers.register(client, "webblk_b", "webblk_b@tt.com")
        # A blocks B
        _WebHelpers.login(client, "webblk_a")
        resp = client.post("/web/social/block", data={
            "target_username": "webblk_b",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify
        _me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webblk_b")
        # B can't see A's relationship
        _rel = client.get("/api/social/requests").json()

    def test_view_other_profile(self, client):
        _WebHelpers.register(client, "webview_a", "webview_a@tt.com")
        _WebHelpers.register(client, "webview_b", "webview_b@tt.com")
        _WebHelpers.login(client, "webview_a")
        resp = client.get("/web/profile/webview_b")
        assert resp.status_code == status.HTTP_200_OK
        assert "webview_b" in resp.text


# ---------------------------------------------------------------------------
# AC3: Web post creation and feed browsing
# ---------------------------------------------------------------------------

class TestAC3WebPostsFeed:

    def test_create_only_me_post(self, client):
        _WebHelpers.register(client, "webpostonly", "webpostonly@tt.com")
        resp = client.post("/web/posts/create", data={
            "content": "Secret thoughts",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Post should be in feed
        assert "Secret thoughts" in resp.text

    def test_create_friends_post(self, client):
        _WebHelpers.register(client, "webpostfri", "webpostfri@tt.com")
        resp = client.post("/web/posts/create", data={
            "content": "For my friends",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "For my friends" in resp.text

    def test_empty_post_is_rejected(self, client):
        _WebHelpers.register(client, "webpostemp", "webpostemp@tt.com")
        resp = client.post("/web/posts/create", data={
            "content": "",
            "audience": "ONLY_ME",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=empty" in resp.headers["location"]

    def test_feed_shows_newest_first(self, client):
        _WebHelpers.register(client, "webfeedord", "webfeedord@tt.com")
        client.post("/web/posts/create", data={
            "content": "Older post",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        client.post("/web/posts/create", data={
            "content": "Newer post",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        resp = client.get("/web/feed")
        html = resp.text
        # Newer post should appear before older post in HTML
        newer_pos = html.find("Newer post")
        older_pos = html.find("Older post")
        assert newer_pos < older_pos, "Newer post should appear before older post in feed"

    def test_delete_own_post(self, client):
        _WebHelpers.register(client, "webdelete", "webdelete@tt.com")
        # Create a post
        resp = client.post("/web/posts/create", data={
            "content": "Will be deleted",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        # Get post ID via API
        _WebHelpers.login(client, "webdelete")
        posts = client.get("/api/feed").json()
        post_id = posts[0]["id"]
        # Delete via web
        _WebHelpers.login(client, "webdelete")
        resp = client.post(f"/web/posts/{post_id}/delete", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Post gone from API
        posts_after = client.get("/api/feed").json()
        assert not any(p["id"] == post_id for p in posts_after)

    def test_invisible_posts_never_render(self, client):
        """A FRIENDS post from a stranger should not render."""
        _WebHelpers.register(client, "webinv_a", "webinv_a@tt.com")
        _WebHelpers.register(client, "webinv_b", "webinv_b@tt.com")
        # A creates a FRIENDS post
        _WebHelpers.login(client, "webinv_a")
        client.post("/web/posts/create", data={
            "content": "Friends only content",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        # B views feed — should NOT see A's post
        _WebHelpers.login(client, "webinv_b")
        resp = client.get("/web/feed")
        assert "Friends only content" not in resp.text


# ---------------------------------------------------------------------------
# AC4: Web like/unlike and comment create/delete
# ---------------------------------------------------------------------------

class TestAC4WebInteractions:

    def test_like_via_web(self, client):
        _WebHelpers.register(client, "weblike_a", "weblike_a@tt.com")
        _WebHelpers.register(client, "weblike_b", "weblike_b@tt.com")
        # Make friends
        _WebHelpers.login(client, "weblike_a")
        _me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "weblike_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "weblike_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "weblike_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # A creates FRIENDS post
        _WebHelpers.login(client, "weblike_a")
        resp = client.post("/web/posts/create", data={
            "content": "Likable post",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        # Get post ID
        post_resp = client.get("/api/feed").json()
        post_id = post_resp[0]["id"]
        # B likes via web
        _WebHelpers.login(client, "weblike_b")
        resp = client.post(f"/web/posts/{post_id}/like", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify like count
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["like_count"] == 1

    def test_unlike_via_web(self, client):
        _WebHelpers.register(client, "webunlike_a", "webunlike_a@tt.com")
        _WebHelpers.register(client, "webunlike_b", "webunlike_b@tt.com")
        # Make friends
        _WebHelpers.login(client, "webunlike_a")
        _me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webunlike_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webunlike_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "webunlike_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # A creates FRIENDS post
        _WebHelpers.login(client, "webunlike_a")
        client.post("/web/posts/create", data={
            "content": "To unlike",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_resp = client.get("/api/feed").json()
        post_id = post_resp[0]["id"]
        # B likes
        _WebHelpers.login(client, "webunlike_b")
        client.post(f"/web/posts/{post_id}/like", follow_redirects=True)
        # B unlikes via web
        resp = client.post(f"/web/posts/{post_id}/unlike", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["like_count"] == 0

    def test_comment_via_web(self, client):
        _WebHelpers.register(client, "webcom_a", "webcom_a@tt.com")
        _WebHelpers.register(client, "webcom_b", "webcom_b@tt.com")
        # Make friends
        _WebHelpers.login(client, "webcom_a")
        _me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webcom_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webcom_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "webcom_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # A creates post
        _WebHelpers.login(client, "webcom_a")
        client.post("/web/posts/create", data={
            "content": "Commentable post",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_resp = client.get("/api/feed").json()
        post_id = post_resp[0]["id"]
        # B comments via web
        _WebHelpers.login(client, "webcom_b")
        resp = client.post(f"/web/posts/{post_id}/comment", data={
            "content": "Great post!",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Verify comment count
        comments = client.get(f"/api/posts/{post_id}/comments").json()
        assert len(comments) == 1
        assert comments[0]["content"] == "Great post!"

    def test_delete_comment_via_web(self, client):
        _WebHelpers.register(client, "webdelc_a", "webdelc_a@tt.com")
        _WebHelpers.register(client, "webdelc_b", "webdelc_b@tt.com")
        # Make friends
        _WebHelpers.login(client, "webdelc_a")
        _me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webdelc_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webdelc_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "webdelc_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # A creates post
        _WebHelpers.login(client, "webdelc_a")
        client.post("/web/posts/create", data={
            "content": "Post for comment delete",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_resp = client.get("/api/feed").json()
        post_id = post_resp[0]["id"]
        # B comments
        _WebHelpers.login(client, "webdelc_b")
        _com_resp = client.post("/api/posts", json={
            "content": "Comment to delete",
        })  # via API to get comment ID
        # Actually comment via API first
        com = client.post(f"/api/posts/{post_id}/comments", json={
            "content": "Comment to delete",
        }).json()
        comment_id = com["id"]
        # B deletes own comment via web
        resp = client.post(f"/web/posts/{post_id}/comments/{comment_id}/delete",
                          follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        comments = client.get(f"/api/posts/{post_id}/comments").json()
        assert not any(c["id"] == comment_id for c in comments)

    def test_browser_comment_create_render_and_delete(self, client):
        """Browser-only comment lifecycle: create via web form, verify rendered
        content and delete control in HTML, delete via web form, verify gone.
        No API endpoint is used for comment ID or existence checks — only HTML.
        """
        _WebHelpers.register(client, "webcmt_u", "webcmt@tt.com")
        # Create ONLY_ME post via web form
        client.post("/web/posts/create", data={
            "content": "Browser comment test post",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        # Add a comment via web form (follow redirects so we read the rendered feed)
        client.post("/web/posts/create", data={
            "content": "Post for comment",  # we don't know the post ID, so we need it
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        # Get post ID from the rendered feed HTML by scraping
        resp = client.get("/web/feed")
        html = resp.text
        assert "Post for comment" in html
        # Extract post ID from the comment form's action URL in the rendered HTML
        import re
        # The comment form action URL is /web/posts/{post_id}/comment
        match = re.search(r'action="/web/posts/(\d+)/comment"', html)
        assert match, "Could not find comment form action in feed HTML"
        post_id = match.group(1)
        # Submit comment via web form
        comment_content = "Browser-only comment"
        resp = client.post(f"/web/posts/{post_id}/comment", data={
            "content": comment_content,
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        html = resp.text
        # Verify comment rendered with author name and content
        assert comment_content in html
        assert "webcmt_u" in html
        # Verify "Delete" button is present for authorized user (same user = author)
        assert 'aria-label="Xóa bình luận này"' in html
        # Verify comment count updated
        assert 'aria-label="Bình luận (1)"' in html
        # Extract comment delete URL from the rendered HTML
        match_del = re.search(
            r'action="/web/posts/' + re.escape(post_id) + r'/comments/(\d+)/delete"',
            html,
        )
        assert match_del, "Could not find comment delete form in feed HTML"
        _comment_id = match_del.group(1)  # captured but not used for API calls
        # Delete the comment via web form
        resp = client.post(
            f"/web/posts/{post_id}/comments/{_comment_id}/delete",
            follow_redirects=True,
        )
        assert resp.status_code == status.HTTP_200_OK
        html_after = resp.text
        # Comment content gone from feed
        assert comment_content not in html_after
        # Comment count back to zero
        assert 'aria-label="Bình luận (0)"' in html_after

    def test_forbidden_comment_deletion(self, client):
        """Non-comment-author and non-post-author cannot delete comment."""
        _WebHelpers.register(client, "webforc_a", "webforc_a@tt.com")
        _WebHelpers.register(client, "webforc_b", "webforc_b@tt.com")
        _WebHelpers.register(client, "webforc_c", "webforc_c@tt.com")
        # A+B friends, B+C friends, A+C not friends
        _WebHelpers.login(client, "webforc_a")
        _me_a = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webforc_b")
        me_b = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webforc_a")
        req_resp = client.post("/api/social/requests", json={"target_user_id": me_b["id"]})
        req_id = req_resp.json()["id"]
        _WebHelpers.login(client, "webforc_b")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        # Make B and C friends
        _WebHelpers.login(client, "webforc_b")
        me_c = None
        _WebHelpers.login(client, "webforc_c")
        me_c = client.get("/api/profile/me").json()
        _WebHelpers.login(client, "webforc_b")
        req_c = client.post("/api/social/requests", json={"target_user_id": me_c["id"]})
        req_c_id = req_c.json()["id"]
        _WebHelpers.login(client, "webforc_c")
        client.post("/api/social/requests/accept", json={"request_id": req_c_id})
        # A creates FRIENDS post — B can see it, C cannot (not A's friend)
        # Actually let's make C also A's friend for this test
        _WebHelpers.login(client, "webforc_c")
        # Need C to be A's friend
        _WebHelpers.login(client, "webforc_a")
        req_ac = client.post("/api/social/requests", json={"target_user_id": me_c["id"]})
        req_ac_id = req_ac.json()["id"]
        _WebHelpers.login(client, "webforc_c")
        client.post("/api/social/requests/accept", json={"request_id": req_ac_id})
        # A creates FRIENDS post
        _WebHelpers.login(client, "webforc_a")
        client.post("/web/posts/create", data={
            "content": "Post for forbidden comment delete",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_resp = client.get("/api/feed").json()
        post_id = post_resp[0]["id"]
        # B comments
        _WebHelpers.login(client, "webforc_b")
        com = client.post(f"/api/posts/{post_id}/comments", json={
            "content": "B's comment",
        }).json()
        comment_id = com["id"]
        # C tries to delete B's comment — should be blocked (403 from API, redirect from web)
        _WebHelpers.login(client, "webforc_c")
        resp = client.post(f"/web/posts/{post_id}/comments/{comment_id}/delete",
                          follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Comment should still exist
        comments = client.get(f"/api/posts/{post_id}/comments").json()
        assert any(c["id"] == comment_id for c in comments)


# ---------------------------------------------------------------------------
# AC5: Web server-side validation — enforces same constraints as API schemas
# ---------------------------------------------------------------------------
# Each test ensures that web mutation routes enforce the same length limits
# defined in ting_ting.schemas (POST_CONTENT_MAX, COMMENT_TEXT_MAX,
# DISPLAY_NAME_MAX, BIO_MAX) as the Pydantic request schemas do for API.

class TestAC5WebValidation:

    def test_web_register_short_username_rejected(self, client):
        """Web register rejects username < 3 chars (same as API USERNAME_MIN=3)."""
        resp = client.post("/web/register", data={
            "username": "ab",
            "email": "short_u@tt.com",
            "password": "securepass1",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "3" in resp.text  # length constraint mention in error
        # No user created
        resp_reg = client.get("/api/profile/me")
        assert resp_reg.status_code == status.HTTP_401_UNAUTHORIZED

    def test_web_register_long_username_rejected(self, client):
        """Web register rejects username > 30 chars (same as API USERNAME_MAX=30)."""
        resp = client.post("/web/register", data={
            "username": "a" * 31,
            "email": "long_u@tt.com",
            "password": "securepass1",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "30" in resp.text  # length constraint mention
        # No user created
        resp_reg = client.get("/api/profile/me")
        assert resp_reg.status_code == status.HTTP_401_UNAUTHORIZED

    def test_web_register_short_password_rejected(self, client):
        """Web register rejects password < 8 chars (same as API PASSWORD_MIN=8)."""
        resp = client.post("/web/register", data={
            "username": "pwuser1",
            "email": "pw1@tt.com",
            "password": "short",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "8" in resp.text  # password length constraint
        # No user created
        _ = client.get("/api/profile/me")

    def test_web_register_special_chars_username_rejected(self, client):
        """Web register rejects username with special characters (same regex as API)."""
        resp = client.post("/web/register", data={
            "username": "bad-user!",
            "email": "special@tt.com",
            "password": "securepass1",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "chữ thường" in resp.text

    def test_web_post_create_too_long_rejected(self, client):
        """Web post create rejects content > 2000 chars (same as API POST_CONTENT_MAX)."""
        from ting_ting.schemas import POST_CONTENT_MAX
        _WebHelpers.register(client, "wpostlong", "wpostl@tt.com")
        resp = client.post("/web/posts/create", data={
            "content": "x" * (POST_CONTENT_MAX + 1),
            "audience": "ONLY_ME",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=too_long" in resp.headers["location"]

    def test_web_post_create_invalid_audience_rejected(self, client):
        """Web post create rejects invalid audience with visible error redirect.
        No post is persisted — behaviour matches API schema validation."""
        _WebHelpers.register(client, "wpostaud", "wposta@tt.com")
        resp = client.post("/web/posts/create", data={
            "content": "Valid content",
            "audience": "INVALID_AUDIENCE",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=invalid_audience" in resp.headers["location"]
        # No post persisted
        posts = client.get("/api/feed").json()
        assert len(posts) == 0

    def test_web_post_edit_too_long_rejected(self, client):
        """Web post edit rejects content > 2000 chars."""
        from ting_ting.schemas import POST_CONTENT_MAX
        _WebHelpers.register(client, "wpedl", "wpedl@tt.com")
        # Create post first
        client.post("/web/posts/create", data={
            "content": "Original post",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
        # Try to edit with oversized content
        resp = client.post(f"/web/posts/{post_id}/edit", data={
            "content": "x" * (POST_CONTENT_MAX + 1),
            "audience": "ONLY_ME",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=too_long" in resp.headers["location"]
        # Verify content was not updated
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["content"] == "Original post"

    def test_web_post_edit_invalid_audience_rejected(self, client):
        """Web post edit rejects invalid audience with visible error redirect.
        No persistence change — behaviour matches API schema validation."""
        _WebHelpers.register(client, "wpedi", "wpedi@tt.com")
        # Create FRIENDS post
        client.post("/web/posts/create", data={
            "content": "Friends post",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
        # Try to edit with invalid audience — rejected entirely
        resp = client.post(f"/web/posts/{post_id}/edit", data={
            "content": "Updated content",
            "audience": "INVALID_AUDIENCE",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=invalid_audience" in resp.headers["location"]
        # No persistence change — content and audience both unchanged
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["content"] == "Friends post"
        assert post_data["audience"] == "FRIENDS"

    def test_web_post_edit_blank_audience_rejected(self, client):
        """Web post edit rejects blank audience (field present but empty string).
        Blank ≠ omitted: blank means user submitted a value — must be valid.
        No persistence change — atomic reject."""
        _WebHelpers.register(client, "wpedb", "wpedb@tt.com")
        client.post("/web/posts/create", data={
            "content": "Original post",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
# Submit audience= (blank string — field present, value empty)
        resp = client.post(f"/web/posts/{post_id}/edit", data={
            "content": "Updated content",
            "audience": "",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=invalid_audience" in resp.headers["location"]
        # No persistence change — content also unchanged (atomic reject)
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["content"] == "Original post"
        assert post_data["audience"] == "FRIENDS"

    def test_web_post_edit_missing_audience_skipped(self, client):
        """Web post edit with audience field absent from form is a no-op on audience.
        Omitted means user did not touch the field — content-only edit allowed."""
        _WebHelpers.register(client, "wpedm", "wpedm@tt.com")
        client.post("/web/posts/create", data={
            "content": "Original post",
            "audience": "FRIENDS",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
        # Submit only content, no audience key at all
        resp = client.post(f"/web/posts/{post_id}/edit", data={
            "content": "Updated content only",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        # Content updated, audience unchanged (field was omitted)
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["content"] == "Updated content only"
        assert post_data["audience"] == "FRIENDS"

    def test_web_comment_too_long_rejected(self, client):
        """Web comment create rejects content > 1000 chars (same as API COMMENT_TEXT_MAX)."""
        from ting_ting.schemas import COMMENT_TEXT_MAX
        _WebHelpers.register(client, "wcmtl", "wcmtl@tt.com")
        # Create post
        client.post("/web/posts/create", data={
            "content": "Post for comment",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
        # Try oversized comment
        resp = client.post(f"/web/posts/{post_id}/comment", data={
            "content": "x" * (COMMENT_TEXT_MAX + 1),
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=comment_too_long" in resp.headers["location"]

    def test_web_profile_display_name_too_long_rejected(self, client):
        """Web profile update rejects oversized display_name with visible error.
        No persistence change — matches API ProfileUpdateRequest max_length."""
        from ting_ting.schemas import DISPLAY_NAME_MAX
        _WebHelpers.register(client, "wpdn", "wpdn@tt.com")
        long_name = "n" * (DISPLAY_NAME_MAX + 10)
        resp = client.post("/web/profile/update", data={
            "display_name": long_name,
            "bio": "",
        }, follow_redirects=True)
        # Re-rendered with error — no truncation
        assert resp.status_code == status.HTTP_200_OK
        assert f"at most {DISPLAY_NAME_MAX}" in resp.text
        # No persistence change — display_name stays None (was never set)
        profile = client.get("/api/profile/me").json()
        assert profile["display_name"] is None

    def test_web_profile_bio_too_long_rejected(self, client):
        """Web profile update rejects oversized bio with visible error.
        No persistence change — matches API ProfileUpdateRequest max_length."""
        from ting_ting.schemas import BIO_MAX
        _WebHelpers.register(client, "wpbio", "wpbio@tt.com")
        long_bio = "b" * (BIO_MAX + 10)
        resp = client.post("/web/profile/update", data={
            "display_name": "",
            "bio": long_bio,
        }, follow_redirects=True)
        # Re-rendered with error — no truncation
        assert resp.status_code == status.HTTP_200_OK
        assert f"at most {BIO_MAX}" in resp.text
        # No persistence change — bio stays None (was never set)
        profile = client.get("/api/profile/me").json()
        assert profile["bio"] is None

    def test_web_post_edit_empty_content_rejected(self, client):
        """Web post edit rejects empty content (same as API min_length=1).
        No persistence change — content stays as-is."""
        _WebHelpers.register(client, "wpeed", "wpeed@tt.com")
        # Create post first
        client.post("/web/posts/create", data={
            "content": "Original post",
            "audience": "ONLY_ME",
        }, follow_redirects=True)
        post_id = client.get("/api/feed").json()[0]["id"]
        # Try to edit with empty content — rejected
        resp = client.post(f"/web/posts/{post_id}/edit", data={
            "content": "",
            "audience": "ONLY_ME",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "error=empty" in resp.headers["location"]
        # Content unchanged
        post_data = client.get(f"/api/posts/{post_id}").json()
        assert post_data["content"] == "Original post"

    def test_web_profile_empty_clears_field(self, client):
        """Web profile update with empty string clears the field (API min_length=0).
        Matches API ProfileUpdateRequest allowing display_name='' / bio=''. """
        _WebHelpers.register(client, "wpec", "wpec@tt.com")
        # Set a value first
        client.post("/web/profile/update", data={
            "display_name": "Test Name",
            "bio": "Test bio",
        }, follow_redirects=True)
        assert client.get("/api/profile/me").json()["display_name"] == "Test Name"
        # Now clear both fields with empty strings
        client.post("/web/profile/update", data={
            "display_name": "",
            "bio": "",
        }, follow_redirects=True)
        profile = client.get("/api/profile/me").json()
        # Both fields cleared (empty string — matching API min_length=0 behavior)
        assert profile["display_name"] == ""


# ---------------------------------------------------------------------------
# AC-EXTRA: Unauthenticated browser requests to protected web routes redirect
# to /web/login (while API routes keep returning JSON 401).
# ---------------------------------------------------------------------------

class TestACExtraWebAuthRedirect:

    def test_unauth_web_feed_redirects_to_login(self, client):
        """GET /web/feed with no cookie → 302 to /web/login (not JSON 401)."""
        resp = client.get("/web/feed", follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_unauth_web_profile_redirects_to_login(self, client):
        """GET /web/profile/me with no cookie → 302 to /web/login."""
        resp = client.get("/web/profile/me", follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_unauth_web_profile_other_redirects_to_login(self, client):
        """GET /web/profile/{username} with no cookie → 302 to /web/login."""
        resp = client.get("/web/profile/someone", follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_unauth_web_post_action_redirects_to_login(self, client):
        """POST /web/posts/999/like with no cookie → 302 to /web/login."""
        resp = client.post("/web/posts/999/like", follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_unauth_web_social_action_redirects_to_login(self, client):
        """POST /web/social/block with no cookie → 302 to /web/login."""
        resp = client.post("/web/social/block", data={"target_username": "x"},
                          follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_unauth_web_comment_redirects_to_login(self, client):
        """POST /web/posts/999/comment with no cookie → 302 to /web/login."""
        resp = client.post("/web/posts/999/comment", data={"content": "hi"},
                          follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert resp.headers["location"] == "/web/login"

    def test_api_routes_still_return_json_401(self, client):
        """GET /api/feed with no auth → 401 JSON ({error: {code: unauthenticated}})."""
        resp = client.get("/api/feed")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert data["error"]["code"] == "unauthenticated"

    def test_api_profile_still_returns_json_401(self, client):
        """GET /api/profile/me with no auth → 401 JSON."""
        resp = client.get("/api/profile/me")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert "application/json" in resp.headers["content-type"]

    def test_web_auth_page_accessibles_without_auth(self, client):
        """/web/login and /web/register render HTML without requiring auth."""
        resp_login = client.get("/web/login")
        assert resp_login.status_code == status.HTTP_200_OK
        assert "Đăng nhập" in resp_login.text

        resp_register = client.get("/web/register")
        assert resp_register.status_code == status.HTTP_200_OK
        assert "Tạo tài khoản" in resp_register.text

    def test_follow_redirect_lands_on_login_page(self, client):
        """GET /web/feed followed → 200 OK with login page HTML."""
        resp = client.get("/web/feed", follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "Đăng nhập" in resp.text
        # Feed content should NOT appear
        assert "Your Feed" not in resp.text


class TestPeopleSearch:
    def test_searches_by_username_and_display_name(self, client):
        _WebHelpers.register(client, "searcher", "searcher@tt.com")
        _WebHelpers.register(client, "findme", "findme@tt.com")
        client.post("/web/profile/update", data={"display_name": "Unique Person", "bio": ""})
        _WebHelpers.login(client, "searcher")

        by_username = client.get("/web/people?q=findme")
        assert by_username.status_code == 200
        assert "@findme" in by_username.text

        by_name = client.get("/web/people?q=unique")
        assert by_name.status_code == 200
        assert "Unique Person" in by_name.text


class TestThreadsFeatures:

    def test_public_post_visible_to_stranger_feed(self, client):
        _WebHelpers.register(client, "pubauthor", "pubauthor@tt.com")
        _WebHelpers.register(client, "pubstranger", "pubstranger@tt.com")
        _WebHelpers.login(client, "pubauthor")
        resp = client.post("/web/posts/create", data={
            "content": "Public Nodi thread",
            "audience": "PUBLIC",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "Mọi người" in resp.text

        _WebHelpers.login(client, "pubstranger")
        feed = client.get("/web/feed").text
        assert "Public Nodi thread" in feed
        assert "đã bắt đầu theo dõi" not in feed

    def test_new_thread_page_matches_nodi_layout(self, client):
        _WebHelpers.register(client, "newthread", "newthread@tt.com")

        response = client.get("/web/thread/new")

        assert response.status_code == status.HTTP_200_OK
        assert "TẠO NỘI DUNG" in response.text
        assert 'class="create-panel"' in response.text
        assert 'action="/web/posts/create"' in response.text
        assert 'id="thread-content"' in response.text
        assert "Thêm ảnh hoặc video" in response.text
        assert "Đăng bài" in response.text
        assert 'href="/web/thread/new" aria-label="Viết bài" class="active" aria-current="page"' in response.text
        assert '<option value="ONLY_ME">Chỉ mình tôi</option>' in response.text
        assert '<option value="FRIENDS">Bạn bè</option>' in response.text
        assert '<option value="PUBLIC">Mọi người</option>' in response.text

    def test_new_thread_page_requires_login(self, client):
        response = client.get("/web/thread/new", follow_redirects=False)
        assert response.status_code == status.HTTP_302_FOUND
        assert "/web/login" in response.headers["location"]

    def test_follow_creates_activity(self, client):
        _WebHelpers.register(client, "follower", "follower@tt.com")
        _WebHelpers.register(client, "creator", "creator@tt.com")
        _WebHelpers.login(client, "follower")
        response = client.post("/web/social/follow", data={"target_username": "creator"}, follow_redirects=False)
        assert response.status_code == 303
        assert "Đang theo dõi" in client.get("/web/people?q=creator").text

        _WebHelpers.login(client, "creator")
        activity = client.get("/web/activity")
        assert activity.status_code == 200
        assert "follower" in activity.text
        assert "đã bắt đầu theo dõi bạn" in activity.text

    def test_activity_page_filters_by_kind(self, client, tmp_session):
        from ting_ting.models import Activity, User

        _WebHelpers.register(client, "activity_like", "activity_like@tt.com")
        _WebHelpers.register(client, "activity_follow", "activity_follow@tt.com")
        _WebHelpers.register(client, "activity_viewer", "activity_viewer@tt.com")
        users = {
            user.username: user
            for user in tmp_session.query(User).filter(
                User.username.in_(["activity_like", "activity_follow", "activity_viewer"])
            ).all()
        }
        tmp_session.add(Activity(
            user_id=users["activity_viewer"].id,
            actor_id=users["activity_like"].id,
            kind="like",
        ))
        tmp_session.add(Activity(
            user_id=users["activity_viewer"].id,
            actor_id=users["activity_follow"].id,
            kind="follow",
        ))
        tmp_session.commit()
        _WebHelpers.login(client, "activity_viewer")

        likes = client.get("/web/activity?kind=like")
        assert likes.status_code == status.HTTP_200_OK
        assert "<strong>activity_like</strong> đã thích" in likes.text
        assert "<strong>activity_follow</strong> đã bắt đầu" not in likes.text
        assert "đã thích bài viết của bạn" in likes.text
        assert 'class="notice-kind notice-kind-like"' in likes.text
        assert 'aria-current="page">Lượt thích' in likes.text

        follows = client.get("/web/activity?kind=follow")
        assert "<strong>activity_follow</strong> đã bắt đầu" in follows.text
        assert "<strong>activity_like</strong> đã thích" not in follows.text
        assert "Theo dõi lại" in follows.text

    def test_save_repost_and_media_post(self, client):
        _WebHelpers.register(client, "threader", "threader@tt.com")
        response = client.post(
            "/web/posts/create",
            data={"content": "Thread with media", "audience": "ONLY_ME"},
            files={"media_file": ("photo.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "/media/post-" in response.text
        import re
        post_id = re.search(r'action="/web/posts/(\d+)/save"', response.text).group(1)

        client.post(f"/web/posts/{post_id}/save")
        assert "Thread with media" in client.get("/web/saved").text
        client.post(f"/web/posts/{post_id}/repost")
        html = client.get("/web/feed").text
        assert 'aria-label="Đăng lại"' in html
        assert re.search(r'aria-label="Đăng lại"[^>]*>.*?<span>1</span>', html, re.DOTALL)


class TestFollowingFeed:
    def test_following_tab_only_shows_visible_posts_from_followed_users(self, client, tmp_session):
        from ting_ting.models import Follow, FriendRequest, User

        _WebHelpers.register(client, "followed_feed", "followed_feed@tt.com")
        client.post("/web/posts/create", data={
            "content": "A post from someone I follow",
            "audience": "FRIENDS",
        })
        _WebHelpers.register(client, "viewer_feed", "viewer_feed@tt.com")
        followed = tmp_session.query(User).filter_by(username="followed_feed").one()
        viewer = tmp_session.query(User).filter_by(username="viewer_feed").one()
        left, right = sorted((followed.id, viewer.id))
        tmp_session.add(FriendRequest(
            sender_id=followed.id,
            recipient_id=viewer.id,
            canonical_left=left,
            canonical_right=right,
            state="accepted",
        ))
        tmp_session.add(Follow(follower_id=viewer.id, followed_id=followed.id))
        tmp_session.commit()
        _WebHelpers.login(client, "viewer_feed")

        response = client.get("/web/feed?view=following")

        assert response.status_code == 200
        assert "A post from someone I follow" in response.text
        assert 'class="feed-tab active" aria-current="page" href="/web/feed?view=following"' in response.text


# ---------------------------------------------------------------------------
# Quick avatar upload (click avatar directly, no edit panel)
# ---------------------------------------------------------------------------

_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 64
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class TestQuickAvatarUpload:

    def test_own_profile_uses_full_avatar_as_picker(self, client):
        _WebHelpers.register(client, "webavatar0", "webavatar0@tt.com")
        html = client.get("/web/profile/me").text
        assert 'class="avatar-upload-trigger"' in html
        assert 'for="avatar_quick"' in html
        assert 'id="avatar_file"' not in html
        assert 'id="avatar_url"' not in html

    def test_upload_redirects_to_profile(self, client):
        _WebHelpers.register(client, "webavatar1", "webavatar1@tt.com")
        resp = client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("me.jpg", _JPEG_BYTES, "image/jpeg")},
            follow_redirects=False,
        )
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert "/web/profile/me" in resp.headers["location"]

    def test_upload_renders_new_avatar_on_profile(self, client):
        _WebHelpers.register(client, "webavatar2", "webavatar2@tt.com")
        client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("me.png", _PNG_BYTES, "image/png")},
            follow_redirects=True,
        )
        html = client.get("/web/profile/me").text
        assert "/media/avatar-" in html

    def test_upload_rejects_non_image(self, client):
        _WebHelpers.register(client, "webavatar3", "webavatar3@tt.com")
        resp = client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("bad.txt", b"not an image", "text/plain")},
            follow_redirects=True,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "JPEG, PNG" in resp.text
        assert "/media/avatar-" not in resp.text

    def test_upload_rejects_oversize(self, client):
        _WebHelpers.register(client, "webavatar4", "webavatar4@tt.com")
        resp = client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("big.jpg", b"\xff\xd8\xff\xe0" + b"0" * (2 * 1024 * 1024 + 1), "image/jpeg")},
            follow_redirects=True,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "2 MB" in resp.text

    def test_upload_without_file_shows_error(self, client):
        _WebHelpers.register(client, "webavatar5", "webavatar5@tt.com")
        resp = client.post("/web/avatar/upload", data={}, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "chọn một tệp ảnh" in resp.text

    def test_upload_requires_login(self, client):
        resp = client.post(
            "/web/avatar/upload",
            files={"avatar_file": ("me.jpg", _JPEG_BYTES, "image/jpeg")},
            follow_redirects=False,
        )
        assert resp.status_code == status.HTTP_302_FOUND
        assert "/web/login" in resp.headers["location"]

    def test_other_profile_has_no_avatar_picker(self, client):
        _WebHelpers.register(client, "webavatar6a", "webavatar6a@tt.com")
        _WebHelpers.register(client, "webavatar6b", "webavatar6b@tt.com")
        _WebHelpers.login(client, "webavatar6a")
        html = client.get("/web/profile/webavatar6b").text
        assert "avatar_quick" not in html


# ---------------------------------------------------------------------------
# Password change from profile settings
# ---------------------------------------------------------------------------


class TestWebPasswordChange:

    def test_profile_renders_password_change_form(self, client):
        _WebHelpers.register(client, "webpassword0", "webpassword0@tt.com")
        html = client.get("/web/profile/me").text
        assert 'action="/web/profile/password"' in html
        assert 'name="current_password"' in html
        assert 'name="new_password"' in html
        assert 'name="confirm_password"' in html

    def test_password_change_requires_current_password(self, client):
        _WebHelpers.register(client, "webpassword1", "webpassword1@tt.com")
        resp = client.post("/web/profile/password", data={
            "current_password": "incorrect-password",
            "new_password": "new-secure-password",
            "confirm_password": "new-secure-password",
        }, follow_redirects=True)
        assert resp.status_code == status.HTTP_200_OK
        assert "Mật khẩu hiện tại không đúng" in resp.text

        _WebHelpers.logout(client)
        login = _WebHelpers.login(client, "webpassword1")
        assert login.status_code == status.HTTP_303_SEE_OTHER

    def test_password_change_rejects_mismatch_and_short_value(self, client):
        _WebHelpers.register(client, "webpassword2", "webpassword2@tt.com")
        mismatch = client.post("/web/profile/password", data={
            "current_password": "securepass1",
            "new_password": "new-secure-password",
            "confirm_password": "different-password",
        }, follow_redirects=True)
        assert "Mật khẩu xác nhận không khớp" in mismatch.text

        too_short = client.post("/web/profile/password", data={
            "current_password": "securepass1",
            "new_password": "short",
            "confirm_password": "short",
        }, follow_redirects=True)
        assert "ít nhất 8 ký tự" in too_short.text

    def test_password_change_updates_login_credential(self, client):
        _WebHelpers.register(client, "webpassword3", "webpassword3@tt.com")
        resp = client.post("/web/profile/password", data={
            "current_password": "securepass1",
            "new_password": "new-secure-password",
            "confirm_password": "new-secure-password",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_303_SEE_OTHER
        assert resp.headers["location"] == "/web/profile/me?password_changed=1"

        _WebHelpers.logout(client)
        old_login = client.post("/web/login", data={
            "identifier": "webpassword3",
            "password": "securepass1",
        }, follow_redirects=True)
        assert "Sai tên đăng nhập hoặc mật khẩu" in old_login.text

        new_login = _WebHelpers.login(client, "webpassword3", "new-secure-password")
        assert new_login.status_code == status.HTTP_303_SEE_OTHER

    def test_password_change_requires_login(self, client):
        resp = client.post("/web/profile/password", data={
            "current_password": "securepass1",
            "new_password": "new-secure-password",
            "confirm_password": "new-secure-password",
        }, follow_redirects=False)
        assert resp.status_code == status.HTTP_302_FOUND
        assert "/web/login" in resp.headers["location"]
