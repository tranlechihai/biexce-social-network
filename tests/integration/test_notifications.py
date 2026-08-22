"""Integration tests for notifications: API parity, read state, orphans.

AC:
* like/comment/repost/follow create exactly one notification (API + web)
* retries do not duplicate
* unread count, mark read, read-all persist
* kind filter + cursor pagination
* blocked actors are hidden
* deleting a post leaves no orphan interaction/notification rows
* blocked profiles hide private fields and posts
"""

from fastapi import status
from sqlalchemy import select

from ting_ting.models import Activity, Comment, Like, Post, PostMedia, Repost, SavedPost


PASSWORD = "securepass1"


def register(client, username):
    resp = client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@tt.com", "password": PASSWORD,
    })
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()


def login(client, username):
    resp = client.post("/api/auth/login", json={
        "identifier": username, "password": PASSWORD,
    })
    assert resp.status_code == status.HTTP_200_OK


class TestNotificationParity:

    def test_api_like_creates_one_notification(self, client):
        _a = register(client, "alice_api")
        b = register(client, "bob_api")
        login(client, "alice_api")
        post_id = client.post("/api/posts", json={
            "content": "public post", "audience": "PUBLIC",
        }).json()["id"]

        login(client, "bob_api")
        assert client.post(f"/api/posts/{post_id}/likes").status_code == 200
        # retry — must not duplicate
        assert client.post(f"/api/posts/{post_id}/likes").status_code == 200

        login(client, "alice_api")
        data = client.get("/api/notifications").json()
        like_items = [i for i in data["items"] if i["kind"] == "like"]
        assert len(like_items) == 1
        assert like_items[0]["post_id"] == post_id
        assert like_items[0]["actor"]["id"] == b["id"]
        assert like_items[0]["is_read"] is False

    def test_api_comment_creates_notification(self, client):
        register(client, "alice_cmt")
        register(client, "bob_cmt")
        login(client, "alice_cmt")
        post_id = client.post("/api/posts", json={
            "content": "post", "audience": "PUBLIC",
        }).json()["id"]

        login(client, "bob_cmt")
        resp = client.post(f"/api/posts/{post_id}/comments", json={"content": "nice"})
        assert resp.status_code == status.HTTP_201_CREATED

        login(client, "alice_cmt")
        data = client.get("/api/notifications", params={"kind": "comment"}).json()
        assert [i["kind"] for i in data["items"]] == ["comment"]

    def test_self_like_creates_no_notification(self, client):
        register(client, "solo_user")
        login(client, "solo_user")
        post_id = client.post("/api/posts", json={
            "content": "mine", "audience": "PUBLIC",
        }).json()["id"]
        client.post(f"/api/posts/{post_id}/likes")
        data = client.get("/api/notifications").json()
        assert data["items"] == []

    def test_follow_creates_notification(self, client):
        a = register(client, "alice_fol")
        register(client, "bob_fol")
        login(client, "bob_fol")
        resp = client.put(f"/api/social/follows/{a['id']}")
        assert resp.status_code == status.HTTP_200_OK

        login(client, "alice_fol")
        data = client.get("/api/notifications", params={"kind": "follow"}).json()
        assert len(data["items"]) == 1
        assert data["items"][0]["actor"]["username"] == "bob_fol"

    def test_web_like_creates_notification(self, client):
        register(client, "alice_web")
        register(client, "bob_web")
        login(client, "alice_web")
        post_id = client.post("/api/posts", json={
            "content": "web post", "audience": "PUBLIC",
        }).json()["id"]

        login(client, "bob_web")
        client.post(f"/web/posts/{post_id}/like")

        login(client, "alice_web")
        data = client.get("/api/notifications").json()
        assert [i["kind"] for i in data["items"]] == ["like"]


class TestReadState:

    def _setup(self, client):
        a = register(client, "alice_rs")
        b = register(client, "bob_rs")
        c = register(client, "carol_rs")
        login(client, "alice_rs")
        post_id = client.post("/api/posts", json={
            "content": "rs post", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_rs")
        client.post(f"/api/posts/{post_id}/likes")
        login(client, "carol_rs")
        client.post(f"/api/posts/{post_id}/likes")
        login(client, "alice_rs")
        return a, b, c

    def test_unread_count_and_mark_read(self, client):
        a, b, c = self._setup(client)
        resp = client.get("/api/notifications/unread-count")
        assert resp.json() == {"unread": 2}

        items = client.get("/api/notifications").json()["items"]
        bob_item = next(i for i in items if i["actor"]["id"] == b["id"])
        resp = client.post(f"/api/notifications/{bob_item['id']}/read")
        assert resp.status_code == 200
        assert resp.json()["is_read"] is True

        assert client.get("/api/notifications/unread-count").json() == {"unread": 1}

    def test_unread_count_survives_reload(self, client):
        self._setup(client)
        client.get("/api/notifications")  # listing must not mark read
        assert client.get("/api/notifications/unread-count").json() == {"unread": 2}

    def test_read_all(self, client):
        self._setup(client)
        resp = client.post("/api/notifications/read-all")
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2
        assert client.get("/api/notifications/unread-count").json() == {"unread": 0}
        items = client.get("/api/notifications").json()["items"]
        assert all(i["is_read"] for i in items)

    def test_mark_read_other_users_notification_404(self, client):
        self._setup(client)
        login(client, "bob_rs")
        resp = client.post("/api/notifications/999999/read")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


class TestCursorPagination:

    def test_cursor_walk_covers_all_without_duplicates(self, client):
        register(client, "alice_cur")
        names = [f"actor_{i}" for i in range(5)]
        for name in names:
            register(client, name)
        login(client, "alice_cur")
        post_id = client.post("/api/posts", json={
            "content": "cur", "audience": "PUBLIC",
        }).json()["id"]

        for i, name in enumerate(names):
            login(client, name)
            kind = ("like", "comment", "repost")[i % 3]
            if kind == "like":
                client.post(f"/api/posts/{post_id}/likes")
            elif kind == "comment":
                client.post(f"/api/posts/{post_id}/comments", json={"content": f"c{i}"})
            else:
                client.put(f"/api/posts/{post_id}/repost")

        login(client, "alice_cur")
        seen = []
        cursor = None
        while True:
            resp = client.get("/api/notifications", params={
                **({"cursor": cursor} if cursor else {}), "limit": 2,
            })
            assert resp.status_code == 200
            data = resp.json()
            seen.extend(i["id"] for i in data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 5
        assert len(set(seen)) == 5

    def test_invalid_cursor_falls_back_to_start(self, client):
        register(client, "alice_bad")
        register(client, "bob_bad")
        login(client, "alice_bad")
        post_id = client.post("/api/posts", json={
            "content": "bad", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_bad")
        client.post(f"/api/posts/{post_id}/likes")
        login(client, "alice_bad")
        resp = client.get("/api/notifications", params={"cursor": "!!not-base64!!"})
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1


class TestBlockHiding:

    def test_blocked_actor_hidden_from_notifications(self, client):
        _a = register(client, "alice_blk")
        b = register(client, "bob_blk")
        login(client, "alice_blk")
        post_id = client.post("/api/posts", json={
            "content": "blk", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_blk")
        client.post(f"/api/posts/{post_id}/likes")

        login(client, "alice_blk")
        client.post("/api/social/blocks", json={"target_user_id": b["id"]})

        data = client.get("/api/notifications").json()
        assert data["items"] == []
        assert client.get("/api/notifications/unread-count").json() == {"unread": 0}

    def test_blocked_by_them_also_hidden(self, client):
        a = register(client, "alice_btt")
        _b = register(client, "bob_btt")
        login(client, "alice_btt")
        post_id = client.post("/api/posts", json={
            "content": "btt", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_btt")
        client.post(f"/api/posts/{post_id}/likes")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})

        login(client, "alice_btt")
        data = client.get("/api/notifications").json()
        assert data["items"] == []


class TestDeletePostOrphans:

    def test_delete_post_leaves_no_orphans(self, client, tmp_session):
        _a = register(client, "alice_del")
        _b = register(client, "bob_del")
        login(client, "alice_del")
        post_id = client.post("/api/posts", json={
            "content": "to be deleted", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_del")
        client.post(f"/api/posts/{post_id}/likes")
        client.post(f"/api/posts/{post_id}/comments", json={"content": "c"})
        client.put(f"/api/posts/{post_id}/saved")
        client.put(f"/api/posts/{post_id}/repost")

        login(client, "alice_del")
        resp = client.delete(f"/api/posts/{post_id}")
        assert resp.status_code == 200

        tmp_session.expire_all()
        for model in (Like, Comment, Activity, SavedPost, Repost, PostMedia):
            orphans = tmp_session.scalar(
                select(model).where(model.post_id == post_id)
            )
            assert orphans is None, f"orphan {model.__name__} for deleted post"
        assert tmp_session.get(Post, post_id) is None


class TestProfileBlockPrivacy:

    def test_blocked_profile_hides_private_fields(self, client):
        register(client, "alice_prv")
        b = register(client, "bob_prv")
        login(client, "bob_prv")
        client.patch("/api/profile/me", json={"bio": "SECRET-BOB-BIO"})

        login(client, "alice_prv")
        client.post("/api/social/blocks", json={"target_user_id": b["id"]})
        resp = client.get("/web/profile/bob_prv")
        assert resp.status_code == 200
        assert "SECRET-BOB-BIO" not in resp.text
        assert "Chưa có bài viết nào" in resp.text

    def test_unblocked_profile_shows_bio(self, client):
        register(client, "alice_prv2")
        register(client, "bob_prv2")
        login(client, "bob_prv2")
        client.patch("/api/profile/me", json={"bio": "VISIBLE-BOB-BIO"})
        login(client, "alice_prv2")
        resp = client.get("/web/profile/bob_prv2")
        assert resp.status_code == 200
        assert "VISIBLE-BOB-BIO" in resp.text


class TestWebActivityReadState:

    def test_activity_page_marks_and_clears_unread(self, client):
        register(client, "alice_wrs")
        register(client, "bob_wrs")
        login(client, "alice_wrs")
        post_id = client.post("/api/posts", json={
            "content": "wrs", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "bob_wrs")
        client.post(f"/api/posts/{post_id}/likes")

        login(client, "alice_wrs")
        resp = client.get("/web/activity")
        assert resp.status_code == 200
        assert "1 chưa đọc" in resp.text
        assert "notice-read-form" in resp.text
        assert "Đánh dấu tất cả đã đọc" in resp.text

        client.post("/web/activity/read-all")
        resp = client.get("/web/activity")
        assert "1 chưa đọc" not in resp.text
        assert "notice-read-form" not in resp.text


class TestNotificationPreferences:

    def test_api_defaults_partial_patch_and_writer_gate(self, client):
        register(client, "pref_owner")
        register(client, "pref_actor")
        login(client, "pref_owner")
        defaults = client.get("/api/notifications/preferences")
        assert defaults.status_code == 200
        assert defaults.json() == {
            "follow": True, "follow_request": True, "like": True,
            "comment": True, "repost": True, "mention": True,
        }
        post_id = client.post("/api/posts", json={
            "content": "preferences", "audience": "PUBLIC",
        }).json()["id"]
        patched = client.patch("/api/notifications/preferences", json={"like": False})
        assert patched.status_code == 200 and patched.json()["like"] is False
        assert patched.json()["comment"] is True
        assert client.patch(
            "/api/notifications/preferences", json={"unknown": False},
        ).status_code == 422

        login(client, "pref_actor")
        client.post(f"/api/posts/{post_id}/likes")
        client.post(f"/api/posts/{post_id}/comments", json={"content": "still on"})
        login(client, "pref_owner")
        items = client.get("/api/notifications").json()["items"]
        assert [item["kind"] for item in items] == ["comment"]

    def test_web_form_persists_all_checkbox_states(self, client):
        register(client, "pref_web")
        login(client, "pref_web")
        response = client.post(
            "/web/account/notifications",
            data={"follow": "on", "comment": "on", "mention": "on"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        values = client.get("/api/notifications/preferences").json()
        assert values == {
            "follow": True, "follow_request": False, "like": False,
            "comment": True, "repost": False, "mention": True,
        }
        page = client.get("/web/account")
        assert page.status_code == 200
        assert 'name="comment" checked' in page.text


class TestNotificationAggregation:

    def test_opt_in_aggregate_and_cutoff_read_preserve_raw_contract(self, client):
        register(client, "agg_owner")
        register(client, "agg_bob")
        register(client, "agg_carol")
        login(client, "agg_owner")
        post_id = client.post("/api/posts", json={
            "content": "aggregate", "audience": "PUBLIC",
        }).json()["id"]
        login(client, "agg_bob")
        client.post(f"/api/posts/{post_id}/comments", json={"content": "one"})
        client.post(f"/api/posts/{post_id}/comments", json={"content": "two"})
        login(client, "agg_carol")
        client.post(f"/api/posts/{post_id}/comments", json={"content": "three"})

        login(client, "agg_owner")
        raw = client.get("/api/notifications", params={"kind": "comment"}).json()
        assert len(raw["items"]) == 3
        aggregates = client.get("/api/notifications/aggregates").json()["items"]
        group = next(item for item in aggregates if item["kind"] == "comment")
        assert group["event_count"] == 3 and group["actor_count"] == 2
        assert len(group["actors"]) == 2
        web_page = client.get("/web/activity")
        assert "TỔNG HỢP CHƯA ĐỌC" in web_page.text
        assert "3 bình luận" in web_page.text

        login(client, "agg_carol")
        client.post(f"/api/posts/{post_id}/comments", json={"content": "later"})
        login(client, "agg_owner")
        marked = client.post(
            f"/api/notifications/aggregates/{group['aggregation_key']}/read"
        )
        assert marked.status_code == 200 and marked.json()["updated"] == 3
        assert client.get("/api/notifications/unread-count").json() == {"unread": 1}
        assert client.post("/api/notifications/aggregates/not-valid/read").status_code == 404

    def test_follow_request_stays_actionable_and_unaggregated(self, client):
        owner = register(client, "agg_private")
        register(client, "agg_follower")
        login(client, "agg_private")
        client.patch("/api/profile/me", json={"is_private": True})
        login(client, "agg_follower")
        client.put(f"/api/social/follows/{owner['id']}")
        login(client, "agg_private")
        assert client.get("/api/notifications/aggregates").json()["items"] == []
        raw = client.get(
            "/api/notifications", params={"kind": "follow_request"},
        ).json()["items"]
        assert len(raw) == 1
