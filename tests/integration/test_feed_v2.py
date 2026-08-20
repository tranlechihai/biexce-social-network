"""Integration tests for Feed V2 — cursors, following feed, response fields.

AC:
* GET /api/feed cursor walk (X-Next-Cursor header) covers all visible posts
* GET /api/feed/following returns posts of followed users + public reposts
* PostResponse includes media, repost_count, saved_by_viewer,
  reposted_by_viewer
* web following tab renders reposted public posts of followed users
"""

from fastapi import status


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


def create_post(client, audience="PUBLIC", content="t"):
    resp = client.post("/api/posts", json={"content": content, "audience": audience})
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()


class TestFeedCursor:

    def test_cursor_walk_covers_all_visible_posts(self, client):
        register(client, "alice_cursor")
        authors = [f"cra_{i}" for i in range(4)]
        for name in authors:
            register(client, name)
        login(client, "alice_cursor")
        for i in range(7):
            login(client, authors[i % 4])
            create_post(client, audience="PUBLIC", content=f"post{i}")

        login(client, "alice_cursor")
        seen = []
        cursor = None
        while True:
            params = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/api/feed", params=params)
            assert resp.status_code == 200
            seen.extend(item["id"] for item in resp.json())
            cursor = resp.headers.get("X-Next-Cursor")
            if cursor is None:
                break
        assert len(seen) == 7 == len(set(seen))

    def test_malformed_cursor_falls_back(self, client):
        register(client, "alice_badc")
        login(client, "alice_badc")
        create_post(client, audience="PUBLIC", content="x")
        resp = client.get("/api/feed", params={"cursor": "!!nope!!"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_legacy_offset_still_works(self, client):
        register(client, "alice_off")
        login(client, "alice_off")
        for i in range(5):
            create_post(client, audience="PUBLIC", content=f"o{i}")
        page1 = client.get("/api/feed", params={"limit": 2, "offset": 0}).json()
        page2 = client.get("/api/feed", params={"limit": 2, "offset": 2}).json()
        assert {p["id"] for p in page1}.isdisjoint({p["id"] for p in page2})


class TestFollowingFeed:

    def test_following_feed_contains_posts_of_followed(self, client):
        _a = register(client, "alice_fol2")
        b = register(client, "bob_fol2")
        _c = register(client, "carol_fol2")
        login(client, "bob_fol2")
        create_post(client, audience="PUBLIC", content="bob thread")
        login(client, "carol_fol2")
        create_post(client, audience="PUBLIC", content="carol thread")
        login(client, "alice_fol2")
        client.put(f"/api/social/follows/{b['id']}")

        feed = client.get("/api/feed/following").json()
        assert [item["content"] for item in feed] == ["bob thread"]

    def test_following_feed_includes_public_reposts(self, client):
        _a = register(client, "alice_rep")
        r = register(client, "reposter")
        _d = register(client, "author")
        login(client, "author")
        post = create_post(client, audience="PUBLIC", content="repostable")
        login(client, "reposter")
        client.put(f"/api/posts/{post['id']}/repost")

        login(client, "alice_rep")
        client.put(f"/api/social/follows/{r['id']}")
        feed = client.get("/api/feed/following").json()
        assert [item["content"] for item in feed] == ["repostable"]
        assert feed[0]["repost_count"] == 1

    def test_following_feed_applies_visibility(self, client):
        _a = register(client, "alice_vis")
        b = register(client, "bob_vis")
        login(client, "bob_vis")
        create_post(client, audience="ONLY_ME", content="hidden")
        create_post(client, audience="FRIENDS", content="friends only")
        login(client, "alice_vis")
        client.put(f"/api/social/follows/{b['id']}")
        feed = client.get("/api/feed/following").json()
        assert feed == []

    def test_anonymous_401(self, client):
        resp = client.get("/api/feed/following")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


class TestPostResponseFields:

    def test_feed_items_carry_viewer_state_and_repost_count(self, client):
        _a = register(client, "alice_rf")
        b = register(client, "bob_rf")
        login(client, "bob_rf")
        post = create_post(client, audience="PUBLIC", content="with state")
        client.put(f"/api/posts/{post['id']}/repost")
        client.put(f"/api/posts/{post['id']}/saved")

        login(client, "alice_rf")
        client.put(f"/api/social/follows/{b['id']}")
        feed = client.get("/api/feed/following").json()
        item = feed[0]
        assert item["content"] == "with state"
        assert item["repost_count"] == 1
        assert item["reposted_by_viewer"] is False
        assert item["saved_by_viewer"] is False
        assert item["liked_by_viewer"] is False

        # viewer-specific flags flip after acting
        client.post(f"/api/posts/{post['id']}/likes")
        client.put(f"/api/posts/{post['id']}/saved")
        client.put(f"/api/posts/{post['id']}/repost")
        post_now = client.get(f"/api/posts/{post['id']}").json()
        assert post_now["liked_by_viewer"] is True
        assert post_now["saved_by_viewer"] is True
        assert post_now["reposted_by_viewer"] is True
        assert post_now["repost_count"] == 2

    def test_comment_cursor(self, client):
        _a = register(client, "alice_cc")
        _b = register(client, "bob_cc")
        login(client, "alice_cc")
        post = create_post(client, audience="PUBLIC", content="comments here")
        login(client, "bob_cc")
        for i in range(4):
            resp = client.post(
                f"/api/posts/{post['id']}/comments",
                json={"content": f"c{i}"},
            )
            assert resp.status_code == status.HTTP_201_CREATED

        login(client, "alice_cc")
        page1 = client.get(
            f"/api/posts/{post['id']}/comments", params={"limit": 2},
        )
        assert len(page1.json()) == 2
        cursor = page1.headers.get("X-Next-Cursor")
        assert cursor
        page2 = client.get(
            f"/api/posts/{post['id']}/comments",
            params={"limit": 2, "cursor": cursor},
        )
        pages = page1.json() + page2.json()
        assert [c["content"] for c in pages] == ["c0", "c1", "c2", "c3"]
        assert page2.headers.get("X-Next-Cursor") is None


class TestWebFollowingTab:

    def test_web_following_tab_renders_reposted_public_post(self, client):
        register(client, "alice_wtab")
        reg_reposter = register(client, "reposter_wtab")
        _reg_author = register(client, "author_wtab")
        login(client, "author_wtab")
        post = create_post(client, audience="PUBLIC", content="wtab thread")
        login(client, "reposter_wtab")
        client.put(f"/api/posts/{post['id']}/repost")
        login(client, "alice_wtab")
        client.put(f"/api/social/follows/{reg_reposter['id']}")

        resp = client.get("/web/feed?view=following")
        assert resp.status_code == 200
        assert "wtab thread" in resp.text

    def test_web_for_you_tab_uses_sql_feed(self, client):
        register(client, "alice_wfy")
        register(client, "bob_wfy")
        login(client, "bob_wfy")
        create_post(client, audience="PUBLIC", content="wfy thread")
        login(client, "alice_wfy")
        resp = client.get("/web/feed")
        assert resp.status_code == 200
        assert "wfy thread" in resp.text
