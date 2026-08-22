"""T-026 mentions, hashtags and privacy-aware native post search."""

from fastapi.testclient import TestClient


def _app():
    from ting_ting.main import app
    return app


def _csrf(c: TestClient) -> dict:
    token = c.cookies.get("ting_ting_csrf")
    return {"X-CSRF-Token": token} if token else {}


def _mutate(c: TestClient, method: str, url: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(_csrf(c))
    return c.request(method, url, headers=headers, **kwargs)


def _user(c: TestClient, name: str) -> dict:
    response = c.post("/api/auth/register", json={
        "username": name,
        "email": f"{name}@t026.com",
        "password": "securepass1",
    })
    assert response.status_code == 201, response.text
    response = c.post("/api/auth/login", json={
        "identifier": name, "password": "securepass1",
    })
    assert response.status_code == 200, response.text
    return c.get(f"/api/users/{name}").json()


def _post(c: TestClient, content: str, audience: str = "PUBLIC") -> dict:
    response = _mutate(c, "POST", "/api/posts", json={
        "content": content, "audience": audience,
    })
    assert response.status_code == 201, response.text
    return response.json()


class TestPostEntities:

    def test_create_returns_entities_and_notifies_visible_mention(self, client):
        author, target = TestClient(_app()), TestClient(_app())
        _user(author, "t026_author1")
        _user(target, "t026_target1")

        post = _post(
            author,
            "Hello @t026_target1 #Python #PYTHON #Việt_Nam",
        )
        assert post["mentions"] == ["t026_target1"]
        assert post["hashtags"] == ["python", "việt_nam"]
        notices = target.get("/api/notifications?kind=mention").json()["items"]
        assert len(notices) == 1
        assert notices[0]["post_id"] == post["id"]
        assert notices[0]["actor"]["username"] == "t026_author1"

    def test_edit_reconciles_entities_and_notifies_only_new_target(self, client):
        author, first, second = TestClient(_app()), TestClient(_app()), TestClient(_app())
        _user(author, "t026_author2")
        _user(first, "t026_first2")
        _user(second, "t026_second2")
        post = _post(author, "Hi @t026_first2 #old")

        edited = _mutate(author, "PATCH", f"/api/posts/{post['id']}", json={
            "content": "Now @t026_second2 #New",
        })
        assert edited.status_code == 200, edited.text
        assert edited.json()["mentions"] == ["t026_second2"]
        assert edited.json()["hashtags"] == ["new"]
        assert len(first.get("/api/notifications?kind=mention").json()["items"]) == 1
        second_items = second.get("/api/notifications?kind=mention").json()["items"]
        assert len(second_items) == 1 and second_items[0]["post_id"] == post["id"]

    def test_invisible_mention_is_indexed_but_does_not_notify(self, client):
        author, target = TestClient(_app()), TestClient(_app())
        _user(author, "t026_author3")
        _user(target, "t026_target3")
        post = _post(author, "Private @t026_target3 #secret", "ONLY_ME")
        assert post["mentions"] == ["t026_target3"]
        assert target.get("/api/notifications?kind=mention").json()["items"] == []
        assert target.get("/api/search/posts", params={"q": "Private"}).json() == []


class TestPostSearch:

    def test_search_tracks_insert_update_delete(self, client):
        owner = TestClient(_app())
        _user(owner, "t026_search1")
        post = _post(owner, "orchid original text")
        found = owner.get("/api/search/posts", params={"q": "orchid"}).json()
        assert [item["id"] for item in found] == [post["id"]]

        edited = _mutate(owner, "PATCH", f"/api/posts/{post['id']}", json={
            "content": "lotus replacement text",
        })
        assert edited.status_code == 200
        assert owner.get("/api/search/posts", params={"q": "orchid"}).json() == []
        assert [item["id"] for item in owner.get(
            "/api/search/posts", params={"q": "lotus"},
        ).json()] == [post["id"]]

        assert _mutate(owner, "DELETE", f"/api/posts/{post['id']}").status_code == 200
        assert owner.get("/api/search/posts", params={"q": "lotus"}).json() == []

    def test_private_pending_active_and_block_visibility(self, client):
        author, viewer = TestClient(_app()), TestClient(_app())
        author_meta = _user(author, "t026_private2")
        _user(viewer, "t026_viewer2")
        _mutate(author, "PATCH", "/api/profile/me", json={"is_private": True})
        post = _post(author, "privategarden searchable")

        assert viewer.get("/api/search/posts", params={"q": "privategarden"}).json() == []
        follow = _mutate(
            viewer, "PUT", f"/api/social/follows/{author_meta['id']}",
        )
        assert follow.json()["state"] == "pending"
        assert viewer.get("/api/search/posts", params={"q": "privategarden"}).json() == []

        request_id = author.get("/api/social/follow-requests").json()[0]["id"]
        _mutate(author, "POST", f"/api/social/follow-requests/{request_id}/approve")
        assert [item["id"] for item in viewer.get(
            "/api/search/posts", params={"q": "privategarden"},
        ).json()] == [post["id"]]

        viewer_id = viewer.get("/api/profile/me").json()["id"]
        _mutate(author, "POST", "/api/social/blocks", json={"target_user_id": viewer_id})
        assert viewer.get("/api/search/posts", params={"q": "privategarden"}).json() == []

    def test_feed_like_search_honors_mute_and_safe_input(self, client):
        author, viewer = TestClient(_app()), TestClient(_app())
        author_meta = _user(author, "t026_muted3")
        _user(viewer, "t026_viewer3")
        post = _post(author, "operatorword visible")
        assert [item["id"] for item in viewer.get(
            "/api/search/posts", params={"q": '"operatorword" OR *'},
        ).json()] == [post["id"]]
        _mutate(viewer, "PUT", f"/api/social/mutes/{author_meta['id']}")
        assert viewer.get("/api/search/posts", params={"q": "operatorword"}).json() == []
        assert viewer.get("/api/search/posts", params={"q": "***"}).json() == []

    def test_search_cursor_is_stable(self, client):
        owner = TestClient(_app())
        _user(owner, "t026_cursor4")
        ids = [_post(owner, f"cursorneedle number {i}")["id"] for i in range(5)]
        page1 = owner.get("/api/search/posts", params={"q": "cursorneedle", "limit": 2})
        page2 = owner.get("/api/search/posts", params={
            "q": "cursorneedle", "limit": 2,
            "cursor": page1.headers["X-Next-Cursor"],
        })
        page3 = owner.get("/api/search/posts", params={
            "q": "cursorneedle", "limit": 2,
            "cursor": page2.headers["X-Next-Cursor"],
        })
        seen = [p["id"] for p in page1.json() + page2.json() + page3.json()]
        assert seen == list(reversed(ids))
        assert page3.headers.get("X-Next-Cursor") is None


class TestHashtagTimeline:

    def test_exact_normalized_tag_and_cursor(self, client):
        owner = TestClient(_app())
        _user(owner, "t026_tag1")
        ids = [_post(owner, f"post {i} #Việt_Nam")["id"] for i in range(3)]
        _post(owner, "other #vietnam")

        first = owner.get("/api/hashtags/VIỆT_NAM/posts", params={"limit": 2})
        assert [p["id"] for p in first.json()] == list(reversed(ids[-2:]))
        second = owner.get("/api/hashtags/việt_nam/posts", params={
            "limit": 2, "cursor": first.headers["X-Next-Cursor"],
        })
        assert [p["id"] for p in second.json()] == [ids[0]]
        assert second.headers.get("X-Next-Cursor") is None

    def test_tag_results_apply_privacy(self, client):
        author, stranger = TestClient(_app()), TestClient(_app())
        _user(author, "t026_tag2")
        _user(stranger, "t026_stranger2")
        _mutate(author, "PATCH", "/api/profile/me", json={"is_private": True})
        _post(author, "hidden #quiettag")
        assert stranger.get("/api/hashtags/quiettag/posts").json() == []


class TestDiscoveryWeb:

    def test_search_and_safe_entity_links_render(self, client):
        author, target = TestClient(_app()), TestClient(_app())
        _user(author, "t026_weba")
        _user(target, "t026_webtarget")
        _post(
            author,
            '<img src=x onerror=alert(1)> webneedle @t026_webtarget #WebTag',
        )

        page = target.get("/web/search", params={"q": "webneedle"})
        assert page.status_code == 200
        assert "&lt;img" in page.text and "<img src=x" not in page.text
        assert 'href="/web/profile/t026_webtarget"' in page.text
        assert 'href="/web/search?tag=webtag"' in page.text
        tagged = target.get("/web/search", params={"tag": "WEBTAG"})
        assert tagged.status_code == 200 and "webneedle" in tagged.text
