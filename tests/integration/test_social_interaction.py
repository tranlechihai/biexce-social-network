"""Integration tests for Increment 4 — Social Interaction APIs.

Covers: comment replies (1-level, notifications to parent author), comment
editing, canceling a sent friend request, user search + public profile +
target followers/following graphs, mutes (feed + notification suppression),
and viewer-side hidden posts.
"""

from fastapi import status


class _Setup:
    PREFIX = "i4"

    @classmethod
    def register(cls, client, username):
        name = f"{cls.PREFIX}_{username}"
        resp = client.post("/api/auth/register", json={
            "username": name,
            "email": f"{name}@i4.com",
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


# ---------------------------------------------------------------------------
# Comment replies
# ---------------------------------------------------------------------------

class TestCommentReplies:

    def test_reply_includes_parent_id(self, client):
        _a, _b = _Setup.register(client, "reply_a"), _Setup.register(client, "reply_b")
        _Setup.login(client, "reply_a")
        post = _Setup.create_post(client, "thread post")
        top = client.post(f"/api/posts/{post['id']}/comments", json={"content": "top"})
        assert top.status_code == status.HTTP_201_CREATED
        assert top.json()["parent_id"] is None

        _Setup.login(client, "reply_b")
        reply = client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "a reply", "parent_comment_id": top.json()["id"]},
        )
        assert reply.status_code == status.HTTP_201_CREATED
        assert reply.json()["parent_id"] == top.json()["id"]

    def test_reply_notifies_parent_author(self, client):
        _a, _b, c = (
            _Setup.register(client, "nfy_a"),
            _Setup.register(client, "nfy_b"),
            _Setup.register(client, "nfy_c"),
        )
        _Setup.login(client, "nfy_a")
        post = _Setup.create_post(client, "notified post")
        _Setup.login(client, "nfy_b")
        top = client.post(f"/api/posts/{post['id']}/comments", json={"content": "top"})
        _Setup.login(client, "nfy_c")
        client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "reply", "parent_comment_id": top.json()["id"]},
        )
        # Post author and parent author both got a comment notification;
        # replying user got none.
        _Setup.login(client, "nfy_b")
        rows = client.get("/api/notifications").json()["items"]
        assert [r["kind"] for r in rows if r["actor"]["id"] == c] == ["comment"]

    def test_reply_to_reply_rejected_422(self, client):
        _a, _b = _Setup.register(client, "deep_a"), _Setup.register(client, "deep_b")
        _Setup.login(client, "deep_a")
        post = _Setup.create_post(client, "deep post")
        top = client.post(f"/api/posts/{post['id']}/comments", json={"content": "t"})
        _Setup.login(client, "deep_b")
        mid = client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "m", "parent_comment_id": top.json()["id"]},
        )
        resp = client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "d", "parent_comment_id": mid.json()["id"]},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.json()["error"]["code"] == "validation"

    def test_reply_to_parent_on_other_post_rejected_422(self, client):
        _a, _b = _Setup.register(client, "xm_a"), _Setup.register(client, "xm_b")
        _Setup.login(client, "xm_a")
        p1 = _Setup.create_post(client, "one")
        p2 = _Setup.create_post(client, "two")
        c1 = client.post(f"/api/posts/{p1['id']}/comments", json={"content": "on p1"})
        _Setup.login(client, "xm_b")
        resp = client.post(
            f"/api/posts/{p2['id']}/comments",
            json={"content": "x", "parent_comment_id": c1.json()["id"]},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_deleting_parent_removes_replies(self, client):
        _a, _b = _Setup.register(client, "del_a"), _Setup.register(client, "del_b")
        _Setup.login(client, "del_a")
        post = _Setup.create_post(client, "delete thread")
        top = client.post(f"/api/posts/{post['id']}/comments", json={"content": "t1"})
        _Setup.login(client, "del_b")
        client.post(
            f"/api/posts/{post['id']}/comments",
            json={"content": "r1", "parent_comment_id": top.json()["id"]},
        )
        _Setup.login(client, "del_a")
        client.delete(f"/api/posts/{post['id']}/comments/{top.json()['id']}")
        remaining = client.get(f"/api/posts/{post['id']}/comments").json()
        assert [c["content"] for c in remaining] == ["t1"] or \
            all(c["content"] != "r1" for c in remaining)


# ---------------------------------------------------------------------------
# Edit comment
# ---------------------------------------------------------------------------

class TestEditComment:

    def test_author_can_edit_own_comment(self, client):
        _a = _Setup.register(client, "edit_a")
        _Setup.login(client, "edit_a")
        post = _Setup.create_post(client, "edit post")
        comment = client.post(
            f"/api/posts/{post['id']}/comments", json={"content": "before"},
        )
        cid = comment.json()["id"]
        resp = client.patch(
            f"/api/posts/{post['id']}/comments/{cid}", json={"content": "after"},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["content"] == "after"

    def test_non_author_cannot_edit_403(self, client):
        _a, _b = _Setup.register(client, "e2_a"), _Setup.register(client, "e2_b")
        _Setup.login(client, "e2_a")
        post = _Setup.create_post(client, "edit2 post")
        comment = client.post(
            f"/api/posts/{post['id']}/comments", json={"content": "mine"},
        )
        cid = comment.json()["id"]
        _Setup.login(client, "e2_b")
        resp = client.patch(
            f"/api/posts/{post['id']}/comments/{cid}", json={"content": "stolen"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_empty_content_rejected_422(self, client):
        _a = _Setup.register(client, "e3_a")
        _Setup.login(client, "e3_a")
        post = _Setup.create_post(client, "edit3 post")
        comment = client.post(
            f"/api/posts/{post['id']}/comments", json={"content": "text"},
        )
        resp = client.patch(
            f"/api/posts/{post['id']}/comments/{comment.json()['id']}",
            json={"content": ""},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# Cancel sent friend request
# ---------------------------------------------------------------------------

class TestCancelSentRequest:

    def test_sender_can_cancel(self, client):
        _a, b = _Setup.register(client, "can_a"), _Setup.register(client, "can_b")
        _Setup.login(client, "can_a")
        req = client.post("/api/social/requests", json={"target_user_id": b})
        assert req.status_code == status.HTTP_201_CREATED
        req_id = req.json()["id"]

        resp = client.delete(f"/api/social/requests/{req_id}")
        assert resp.status_code == status.HTTP_204_NO_CONTENT
        remaining = client.get("/api/social/requests").json()
        assert all(r["id"] != req_id for r in remaining)
        # The pair can send a fresh request afterwards.
        fresh = client.post("/api/social/requests", json={"target_user_id": b})
        assert fresh.status_code == status.HTTP_201_CREATED
        client.delete(f"/api/social/requests/{fresh.json()['id']}")

    def test_recipient_cannot_cancel_403(self, client):
        _a, b = _Setup.register(client, "rc_a"), _Setup.register(client, "rc_b")
        _Setup.login(client, "rc_a")
        req = client.post("/api/social/requests", json={"target_user_id": b})
        req_id = req.json()["id"]
        _Setup.login(client, "rc_b")
        resp = client.delete(f"/api/social/requests/{req_id}")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_cancel_missing_404(self, client):
        _a = _Setup.register(client, "cm_a")
        _Setup.login(client, "cm_a")
        assert client.delete("/api/social/requests/99999").status_code == 404

    def test_cancel_accepted_409(self, client):
        _a, b = _Setup.register(client, "ca_a"), _Setup.register(client, "ca_b")
        _Setup.login(client, "ca_a")
        req = client.post("/api/social/requests", json={"target_user_id": b})
        req_id = req.json()["id"]
        _Setup.login(client, "ca_b")
        assert client.post(
            "/api/social/requests/accept", json={"request_id": req_id},
        ).status_code == status.HTTP_200_OK
        _Setup.login(client, "ca_a")
        assert client.delete(f"/api/social/requests/{req_id}").status_code == 409


# ---------------------------------------------------------------------------
# User public profile + search + graphs
# ---------------------------------------------------------------------------

class TestUserDiscovery:

    def test_public_profile_fields_and_counts(self, client):
        _a, b, c = (
            _Setup.register(client, "pub_a"),
            _Setup.register(client, "pub_b"),
            _Setup.register(client, "pub_c"),
        )
        # a follows b; b follows c; b and c become friends.
        _Setup.login(client, "pub_a")
        client.put(f"/api/social/follows/{b}")
        _Setup.login(client, "pub_b")
        client.put(f"/api/social/follows/{c}")
        req = client.post("/api/social/requests", json={"target_user_id": c})
        # The recipient (c) must accept.
        _Setup.login(client, "pub_c")
        client.post("/api/social/requests/accept", json={"request_id": req.json()["id"]})

        _Setup.login(client, "pub_a")
        resp = client.get("/api/users/i4_pub_b")
        data = resp.json()
        assert resp.status_code == status.HTTP_200_OK
        assert data["relationship"] == "none"
        assert data["follower_count"] == 1
        assert data["following_count"] == 1
        assert data["friend_count"] == 1
        assert data["id"] == b

    def test_block_redacts_public_profile(self, client):
        _a, b = _Setup.register(client, "br_a"), _Setup.register(client, "br_b")
        _Setup.login(client, "br_a")
        client.post("/api/social/blocks", json={"target_user_id": b})
        resp = client.get("/api/users/i4_br_b")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["relationship"] == "blocked_by_me"
        assert data["display_name"] is None
        assert data["bio"] is None
        assert data["follower_count"] is None

    def test_unknown_username_404(self, client):
        _a = _Setup.register(client, "un_a")
        _Setup.login(client, "un_a")
        assert client.get("/api/users/does_not_exist_999").status_code == 404

    def test_search_excludes_viewer_and_blocked_by_them(self, client):
        _a, _b, c = (
            _Setup.register(client, "sea_a"),
            _Setup.register(client, "sea_b"),
            _Setup.register(client, "sea_c"),
        )
        # c blocks a → a must not appear in b's... use: b blocks a, viewer c sees b not a?
        # Simpler: viewer = c; a blocks c (so c sees a as blocked_by_them → excluded).
        _Setup.login(client, "sea_a")
        client.post("/api/social/blocks", json={"target_user_id": c})
        _Setup.login(client, "sea_c")
        items = client.get("/api/users", params={"limit": 20}).json()
        usernames = [i["username"] for i in items]
        assert "i4_sea_c" not in usernames
        assert "i4_sea_a" not in usernames  # blocked the viewer
        assert "i4_sea_b" in usernames

    def test_search_query_filter(self, client):
        _a = _Setup.register(client, "sq_a")
        _zed = _Setup.register(client, "zelphirus")
        _Setup.login(client, "sq_a")
        items = client.get("/api/users", params={"q": "zelph"}).json()
        assert [i["username"] for i in items] == ["i4_zelphirus"]

    def test_search_cursor_walk(self, client):
        _a = _Setup.register(client, "cur_a")
        for extra in ("cur_b2", "cur_c3", "cur_d4"):
            _Setup.register(client, extra)
        _Setup.login(client, "cur_a")
        seen, cursor, pages = [], None, 0
        while True:
            resp = client.get(
                "/api/users", params={"limit": 2, **( {"cursor": cursor} if cursor else {} )},
            )
            assert resp.status_code == status.HTTP_200_OK
            seen.extend(i["username"] for i in resp.json())
            pages += 1
            cursor = resp.headers.get("X-Next-Cursor")
            if not cursor:
                break
        assert pages >= 2
        assert seen == sorted(set(seen))
        assert "i4_cur_b2" in seen and "i4_cur_d4" in seen
        assert "i4_cur_a" not in seen

    def test_followers_and_following_graphs(self, client):
        a, b, _c = (
            _Setup.register(client, "gph_a"),
            _Setup.register(client, "gph_b"),
            _Setup.register(client, "gph_c"),
        )
        _Setup.login(client, "gph_b")
        client.put(f"/api/social/follows/{a}")
        _Setup.login(client, "gph_c")
        client.put(f"/api/social/follows/{a}")
        client.put(f"/api/social/follows/{b}")

        _Setup.login(client, "gph_c")
        followers = client.get("/api/users/i4_gph_a/followers").json()
        assert {u["username"] for u in followers} == {"i4_gph_b", "i4_gph_c"}
        following = client.get("/api/users/i4_gph_b/following").json()
        assert {u["username"] for u in following} == {"i4_gph_a"}

    def test_graphs_404_for_blocked_pair(self, client):
        _a, b = _Setup.register(client, "gb_a"), _Setup.register(client, "gb_b")
        _Setup.login(client, "gb_a")
        client.post("/api/social/blocks", json={"target_user_id": b})
        assert client.get("/api/users/i4_gb_b/followers").status_code == 404
        assert client.get("/api/users/i4_gb_b/following").status_code == 404


# ---------------------------------------------------------------------------
# Mutes — feed + notification suppression
# ---------------------------------------------------------------------------

class TestMutes:

    def test_muted_posts_leave_both_feeds(self, client):
        _a, b = _Setup.register(client, "mute_a"), _Setup.register(client, "mute_b")
        _Setup.login(client, "mute_a")
        client.put(f"/api/social/follows/{b}")
        _Setup.login(client, "mute_b")
        post = _Setup.create_post(client, "muted post")
        _Setup.login(client, "mute_a")

        assert client.put(f"/api/social/mutes/{b}").json()["active"] is True
        feed = client.get("/api/feed").json()
        assert all(p["id"] != post["id"] for p in feed)
        following = client.get("/api/feed/following").json()
        assert all(p["id"] != post["id"] for p in following)

        assert client.delete(f"/api/social/mutes/{b}").json()["active"] is False
        feed = client.get("/api/feed").json()
        assert any(p["id"] == post["id"] for p in feed)

    def test_muted_actor_notifications_hidden(self, client):
        _a, b = _Setup.register(client, "mn_a"), _Setup.register(client, "mn_b")
        # a posts; b likes it → a gets a notification from b (actor = b).
        _Setup.login(client, "mn_a")
        post = _Setup.create_post(client, "like me")
        _Setup.login(client, "mn_b")
        client.post(f"/api/posts/{post['id']}/likes")
        # a mutes b → b's notification disappears from a's inbox.
        _Setup.login(client, "mn_a")
        assert client.put(f"/api/social/mutes/{b}").json()["active"] is True

        items = client.get("/api/notifications").json()["items"]
        assert items == []
        assert client.get("/api/notifications/unread-count").json()["unread"] == 0

        client.delete(f"/api/social/mutes/{b}")
        items = client.get("/api/notifications").json()["items"]
        assert any(i["actor"]["id"] == b and i["kind"] == "like" for i in items)

    def test_mute_idempotent_and_self_rejected(self, client):
        a, b = _Setup.register(client, "mi_a"), _Setup.register(client, "mi_b")
        _Setup.login(client, "mi_a")
        assert client.put(f"/api/social/mutes/{b}").status_code == 200
        assert client.put(f"/api/social/mutes/{b}").status_code == 200
        assert client.delete(f"/api/social/mutes/{b}").status_code == 200
        assert client.delete(f"/api/social/mutes/{b}").status_code == 200
        assert client.put(f"/api/social/mutes/{a}").status_code == 409

    def test_mute_does_not_remove_relationship(self, client):
        _a, b = _Setup.register(client, "mr_a"), _Setup.register(client, "mr_b")
        _Setup.login(client, "mr_a")
        client.put(f"/api/social/follows/{b}")
        client.put(f"/api/social/mutes/{b}")
        following = client.get("/api/social/following").json()
        assert any(u["id"] == b for u in following)
        client.delete(f"/api/social/mutes/{b}")


# ---------------------------------------------------------------------------
# Hidden posts
# ---------------------------------------------------------------------------

class TestHiddenPosts:

    def test_hide_removes_from_feed_but_not_direct_read(self, client):
        _a, _b = _Setup.register(client, "hid_a"), _Setup.register(client, "hid_b")
        _Setup.login(client, "hid_b")
        post = _Setup.create_post(client, "hide me")
        _Setup.login(client, "hid_a")

        assert client.put(f"/api/posts/{post['id']}/hidden").json()["active"] is True
        feed = client.get("/api/feed").json()
        assert all(p["id"] != post["id"] for p in feed)
        # Direct read is unaffected — hiding is a feed preference, not access.
        assert client.get(f"/api/posts/{post['id']}").status_code == 200

        assert client.delete(f"/api/posts/{post['id']}/hidden").json()["active"] is False
        feed = client.get("/api/feed").json()
        assert any(p["id"] == post["id"] for p in feed)

    def test_hide_invisible_post_404(self, client):
        _a, _b = _Setup.register(client, "hi_a"), _Setup.register(client, "hi_b")
        _Setup.login(client, "hi_b")
        post = _Setup.create_post(client, "secret", audience="ONLY_ME")
        _Setup.login(client, "hi_a")
        assert client.put(f"/api/posts/{post['id']}/hidden").status_code == 404

    def test_hide_post_disappears_from_following_feed_too(self, client):
        _a, b = _Setup.register(client, "hf_a"), _Setup.register(client, "hf_b")
        _Setup.login(client, "hf_a")
        client.put(f"/api/social/follows/{b}")
        _Setup.login(client, "hf_b")
        post = _Setup.create_post(client, "follow hide")
        _Setup.login(client, "hf_a")
        client.put(f"/api/posts/{post['id']}/hidden")
        following = client.get("/api/feed/following").json()
        assert all(p["id"] != post["id"] for p in following)
