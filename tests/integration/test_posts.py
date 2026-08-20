"""Integration tests for text posts, audience authorization, and feed.

AC1–AC8 — post CRUD, audience visibility, feed ordering, social-state transitions.
"""

from fastapi import status



class _Setup:
    @staticmethod
    def register(client, username, email, password="securepass1"):
        return client.post("/api/auth/register", json={
            "username": username, "email": email, "password": password,
        })

    @staticmethod
    def login(client, username, password):
        return client.post("/api/auth/login", json={
            "identifier": username, "password": password,
        })

    @staticmethod
    def register_pair(client, prefix="usr"):
        a = _Setup.register(client, f"{prefix}a", f"{prefix}a@tt.com")
        assert a.status_code == status.HTTP_201_CREATED
        b = _Setup.register(client, f"{prefix}b", f"{prefix}b@tt.com")
        assert b.status_code == status.HTTP_201_CREATED
        return a.json(), b.json()

    @staticmethod
    def become_friends(client, a_id, b_id, a_name, b_name):
        """Make two registered users friends (a sends, b accepts)."""
        _Setup.login(client, a_name, "securepass1")
        req_resp = client.post("/api/social/requests", json={"target_user_id": b_id})
        assert req_resp.status_code in (status.HTTP_201_CREATED, status.HTTP_409_CONFLICT)
        req_id = req_resp.json().get("id")
        _Setup.login(client, b_name, "securepass1")
        if req_id:
            client.post("/api/social/requests/accept", json={"request_id": req_id}).json()

    @staticmethod
    def block(client, blocker_name, blocked_id):
        _Setup.login(client, blocker_name, "securepass1")
        return client.post("/api/social/blocks", json={"target_user_id": blocked_id})

    @staticmethod
    def unblock(client, blocker_name, blocked_id):
        _Setup.login(client, blocker_name, "securepass1")
        return client.delete(f"/api/social/blocks/{blocked_id}")

    @staticmethod
    def create_post(client, audience="ONLY_ME", content="Test post"):
        """Create a post (caller must be logged in). Returns post_id."""
        resp = client.post("/api/posts", json={
            "content": content, "audience": audience,
        })
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        return resp.json()["id"]


# ---------------------------------------------------------------------------
# AC1: Post creation
# ---------------------------------------------------------------------------

class TestAC1CreatePost:

    def test_create_with_valid_content(self, client):
        _Setup.register(client, "postuser", "post@tt.com")
        _Setup.login(client, "postuser", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "Hello world",
            "audience": "ONLY_ME",
        })
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["content"] == "Hello world"
        assert data["audience"] == "ONLY_ME"
        assert data["author"]["username"] == "postuser"
        assert "created_at" in data

    def test_create_with_friends_audience(self, client):
        _Setup.register(client, "postuser2", "post2@tt.com")
        _Setup.login(client, "postuser2", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "For my friends",
            "audience": "FRIENDS",
        })
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["audience"] == "FRIENDS"

    def test_empty_content_rejected(self, client):
        _Setup.register(client, "postuser3", "post3@tt.com")
        _Setup.login(client, "postuser3", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "",
            "audience": "ONLY_ME",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_oversized_content_rejected(self, client):
        _Setup.register(client, "postuser4", "post4@tt.com")
        _Setup.login(client, "postuser4", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "x" * 2001,
            "audience": "ONLY_ME",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_invalid_audience_rejected(self, client):
        _Setup.register(client, "postuser5", "post5@tt.com")
        _Setup.login(client, "postuser5", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "valid",
            "audience": "public",
        })
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_lowercase_audience_rejected(self, client):
        """Lowercase/out-of-contract audience values must be rejected (422)."""
        _Setup.register(client, "caseuser", "case@tt.com")
        _Setup.login(client, "caseuser", "securepass1")
        # Contract requires exactly "ONLY_ME" or "FRIENDS" — nothing else passes
        for bad in ("only_me", "friends", "Only_Me", "Friends", "public"):
            resp = client.post("/api/posts", json={
                "content": "test",
                "audience": bad,
            })
            assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, (
                f"Expected 422 for audience={bad!r}, got {resp.status_code}"
            )

    def test_no_duplicate_row_on_validation_failure(self, client, tmp_engine):
        _Setup.register(client, "postuser6", "post6@tt.com")
        _Setup.login(client, "postuser6", "securepass1")
        # First valid post
        client.post("/api/posts", json={"content": "ok", "audience": "ONLY_ME"})
        # Empty content — rejected
        client.post("/api/posts", json={"content": "", "audience": "ONLY_ME"})
        from sqlalchemy import text
        with tmp_engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM posts")).scalar()
        assert count == 1


# ---------------------------------------------------------------------------
# AC2: Post edit and delete
# ---------------------------------------------------------------------------

class TestAC2EditDelete:

    def _create_post(self, client):
        _Setup.register(client, "edituser", "edit@tt.com")
        _Setup.login(client, "edituser", "securepass1")
        resp = client.post("/api/posts", json={
            "content": "original",
            "audience": "ONLY_ME",
        })
        assert resp.status_code == status.HTTP_201_CREATED
        return resp.json()["id"]

    def test_author_edits(self, client):
        post_id = self._create_post(client)
        _Setup.login(client, "edituser", "securepass1")
        resp = client.patch(f"/api/posts/{post_id}", json={
            "content": "edited version",
        })
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["content"] == "edited version"

    def test_author_edits_audience(self, client):
        post_id = self._create_post(client)
        _Setup.login(client, "edituser", "securepass1")
        resp = client.patch(f"/api/posts/{post_id}", json={
            "audience": "FRIENDS",
        })
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["audience"] == "FRIENDS"

    def test_non_author_edit_denied(self, client):
        a, b = _Setup.register_pair(client, "na")
        _Setup.login(client, "naa", "securepass1")
        post_resp = client.post("/api/posts", json={
            "content": "a post", "audience": "FRIENDS",
        })
        post_id = post_resp.json()["id"]
        _Setup.login(client, "nab", "securepass1")
        resp = client.patch(f"/api/posts/{post_id}", json={"content": "hacked"})
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        # Original content unchanged
        _Setup.login(client, "naa", "securepass1")
        get_resp = client.get(f"/api/posts/{post_id}")
        assert get_resp.json()["content"] == "a post"

    def test_author_deletes(self, client):
        post_id = self._create_post(client)
        _Setup.login(client, "edituser", "securepass1")
        resp = client.delete(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_200_OK
        # Post gone
        get_resp = client.get(f"/api/posts/{post_id}")
        assert get_resp.status_code == status.HTTP_404_NOT_FOUND

    def test_non_author_delete_denied(self, client):
        a, b = _Setup.register_pair(client, "da")
        _Setup.login(client, "daa", "securepass1")
        post_resp = client.post("/api/posts", json={
            "content": "a post", "audience": "ONLY_ME",
        })
        post_id = post_resp.json()["id"]
        _Setup.login(client, "dab", "securepass1")
        resp = client.delete(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        # Post still exists
        _Setup.login(client, "daa", "securepass1")
        assert client.get(f"/api/posts/{post_id}").status_code == status.HTTP_200_OK

    def test_deleted_post_not_found(self, client):
        post_id = self._create_post(client)
        _Setup.login(client, "edituser", "securepass1")
        client.delete(f"/api/posts/{post_id}")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# AC3: ONLY_ME visibility
# ---------------------------------------------------------------------------

class TestAC3OnlyMe:

    def test_author_can_read_only_me(self, client):
        _Setup.register(client, "om_author", "oma@tt.com")
        _Setup.login(client, "om_author", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "private", "audience": "ONLY_ME",
        }).json()["id"]
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_200_OK

    def test_friend_cannot_read_only_me(self, client):
        a, b = _Setup.register_pair(client, "om")
        _Setup.login(client, "oma", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "private", "audience": "ONLY_ME",
        }).json()["id"]
        _Setup.become_friends(client, a["id"], b["id"], "oma", "omb")
        _Setup.login(client, "omb", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_non_friend_cannot_read_only_me(self, client):
        a, b = _Setup.register_pair(client, "om2")
        _Setup.login(client, "om2a", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "private", "audience": "ONLY_ME",
        }).json()["id"]
        _Setup.login(client, "om2b", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_blocked_peer_cannot_read_only_me(self, client):
        a, b = _Setup.register_pair(client, "om3")
        _Setup.login(client, "om3a", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "private", "audience": "ONLY_ME",
        }).json()["id"]
        _Setup.block(client, "om3b", a["id"])
        # A is blocked by B — A still sees own post via auth, B cannot
        _Setup.login(client, "om3b", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_only_me_not_in_feed(self, client):
        """ONLY_ME posts should NOT appear in friend's feed."""
        a, b = _Setup.register_pair(client, "omf")
        _Setup.become_friends(client, a["id"], b["id"], "omfa", "omfb")
        # A creates ONLY_ME post
        _Setup.login(client, "omfa", "securepass1")
        client.post("/api/posts", json={"content": "private", "audience": "ONLY_ME"})
        # B views feed
        _Setup.login(client, "omfb", "securepass1")
        feed = client.get("/api/feed").json()
        assert feed == []


# ---------------------------------------------------------------------------
# AC4: FRIENDS visibility
# ---------------------------------------------------------------------------

class TestAC4Friends:

    def test_author_can_read_friends_post(self, client):
        _Setup.register(client, "fr_author", "fra@tt.com")
        _Setup.login(client, "fr_author", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_200_OK

    def test_current_friend_can_read(self, client):
        a, b = _Setup.register_pair(client, "fr")
        _Setup.become_friends(client, a["id"], b["id"], "fra", "frb")
        _Setup.login(client, "fra", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        _Setup.login(client, "frb", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_200_OK

    def test_non_friend_cannot_read_friends_post(self, client):
        a, b = _Setup.register_pair(client, "fr2")
        _Setup.login(client, "fr2a", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        _Setup.login(client, "fr2b", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_blocked_peer_cannot_read_friends_post(self, client):
        a, b = _Setup.register_pair(client, "fr3")
        _Setup.become_friends(client, a["id"], b["id"], "fr3a", "fr3b")
        _Setup.login(client, "fr3a", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        _Setup.block(client, "fr3b", a["id"])
        _Setup.login(client, "fr3b", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# AC5: Social-state transitions affect visibility
# ---------------------------------------------------------------------------

class TestAC5SocialStateTransitions:

    def test_unfriend_removes_friends_post_visibility(self, client):
        a, b = _Setup.register_pair(client, "unf")
        _Setup.become_friends(client, a["id"], b["id"], "unfa", "unfb")
        _Setup.login(client, "unfa", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        # B unfriends — should lose visibility
        _Setup.login(client, "unfb", "securepass1")
        client.post("/api/social/friends/unfriend", json={"target_user_id": a["id"]})
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_block_removes_friends_post_visibility(self, client):
        a, b = _Setup.register_pair(client, "blk")
        _Setup.become_friends(client, a["id"], b["id"], "blka", "blkb")
        _Setup.login(client, "blka", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        }).json()["id"]
        _Setup.block(client, "blkb", a["id"])
        _Setup.login(client, "blkb", "securepass1")
        resp = client.get(f"/api/posts/{post_id}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_feed_omits_after_unfriend(self, client):
        a, b = _Setup.register_pair(client, "udt")
        _Setup.become_friends(client, a["id"], b["id"], "udta", "udtb")
        _Setup.login(client, "udta", "securepass1")
        client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        })
        _Setup.login(client, "udtb", "securepass1")
        client.post("/api/social/friends/unfriend", json={"target_user_id": a["id"]})
        feed = client.get("/api/feed").json()
        assert feed == []

    def test_unblock_does_not_restore_visibility(self, client):
        """Block → unblock does not automatically restore friendship or feed."""
        a, b = _Setup.register_pair(client, "ubs")
        _Setup.become_friends(client, a["id"], b["id"], "ubsa", "ubsb")
        _Setup.login(client, "ubsa", "securepass1")
        client.post("/api/posts", json={
            "content": "for friends", "audience": "FRIENDS",
        })
        _Setup.block(client, "ubsb", a["id"])
        _Setup.unblock(client, "ubsb", a["id"])
        # No friendship restored → no visibility restored
        _Setup.login(client, "ubsb", "securepass1")
        feed = client.get("/api/feed").json()
        assert feed == []


# ---------------------------------------------------------------------------
# AC6: Feed ordering, pagination, visibility filter
# ---------------------------------------------------------------------------

class TestAC6FeedOrdering:

    def test_feed_newest_first(self, client):
        a, b = _Setup.register_pair(client, "ord")
        _Setup.become_friends(client, a["id"], b["id"], "orda", "ordb")
        _Setup.login(client, "orda", "securepass1")
        client.post("/api/posts", json={"content": "old", "audience": "FRIENDS"})
        # Small sleep to ensure distinct timestamps (SQLite precision)
        import time
        time.sleep(0.05)
        client.post("/api/posts", json={"content": "new", "audience": "FRIENDS"})
        _Setup.login(client, "ordb", "securepass1")
        feed = client.get("/api/feed").json()
        assert len(feed) == 2
        assert feed[0]["content"] == "new"
        assert feed[1]["content"] == "old"

    def test_feed_stable_id_tiebreaker(self, client):
        """Posts with controlled equal timestamps ordered by id descending."""
        a, b = _Setup.register_pair(client, "tie")
        _Setup.become_friends(client, a["id"], b["id"], "tiea", "tieb")

        # Get the DB engine to insert with controlled timestamps
        import ting_ting.database as db_mod
        from sqlalchemy import text
        from datetime import datetime, timezone

        # Insert two posts with the EXACT same timestamp via raw SQL
        now = datetime.now(timezone.utc)
        with db_mod._SessionLocal().bind.connect() as conn:
            conn.execute(text(
                "INSERT INTO posts (author_id, content, audience, created_at, updated_at) "
                "VALUES (:aid, :c1, :aud, :ts, :ts)"
            ), {"aid": a["id"], "c1": "first", "aud": "FRIENDS", "ts": now})
            conn.execute(text(
                "INSERT INTO posts (author_id, content, audience, created_at, updated_at) "
                "VALUES (:aid, :c2, :aud, :ts, :ts)"
            ), {"aid": a["id"], "c2": "second", "aud": "FRIENDS", "ts": now})
            conn.commit()

        # Verify both posts have the same timestamp
        with db_mod._SessionLocal().bind.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, content, created_at FROM posts WHERE author_id=:aid ORDER BY id"
            ), {"aid": a["id"]}).fetchall()
        assert rows[0].created_at == rows[1].created_at, (
            f"Timestamps not equal: {rows[0].created_at} vs {rows[1].created_at}"
        )
        first_id = rows[0].id
        second_id = rows[1].id
        assert second_id > first_id

        # Feed must return higher-ID post first when timestamps are equal
        _Setup.login(client, "tieb", "securepass1")
        feed = client.get("/api/feed").json()
        # Both posts are FRIENDS-audience so visible to B
        friend_posts = [p for p in feed if p["content"] in ("first", "second")]
        assert len(friend_posts) == 2
        assert friend_posts[0]["content"] == "second"  # higher id first
        assert friend_posts[1]["content"] == "first"

    def test_feed_limits_enforced(self, client):
        a, b = _Setup.register_pair(client, "lim")
        _Setup.become_friends(client, a["id"], b["id"], "lima", "limb")
        _Setup.login(client, "lima", "securepass1")
        for i in range(10):
            client.post("/api/posts", json={
                "content": f"post{i}", "audience": "FRIENDS",
            })
        _Setup.login(client, "limb", "securepass1")
        feed = client.get("/api/feed?limit=3").json()
        assert len(feed) == 3

    def test_feed_offset_pagination(self, client):
        a, b = _Setup.register_pair(client, "pag")
        _Setup.become_friends(client, a["id"], b["id"], "paga", "pagb")
        _Setup.login(client, "paga", "securepass1")
        for i in range(10):
            client.post("/api/posts", json={
                "content": f"post{i}", "audience": "FRIENDS",
            })
        _Setup.login(client, "pagb", "securepass1")
        page1 = client.get("/api/feed?limit=3&offset=0").json()
        page2 = client.get("/api/feed?limit=3&offset=3").json()
        assert len(page1) == 3
        assert len(page2) == 3
        # No overlap between pages
        ids1 = {p["id"] for p in page1}
        ids2 = {p["id"] for p in page2}
        assert ids1.isdisjoint(ids2)

    def test_feed_excludes_invisible(self, client):
        """Mixed visible/invisible posts — only visible ones appear in feed."""
        a, b = _Setup.register_pair(client, "mix")
        _Setup.become_friends(client, a["id"], b["id"], "mixa", "mixb")
        _Setup.login(client, "mixa", "securepass1")
        # FRIENDS post — visible to B
        client.post("/api/posts", json={"content": "friends post", "audience": "FRIENDS"})
        # ONLY_ME post — not visible to B
        client.post("/api/posts", json={"content": "own post", "audience": "ONLY_ME"})
        _Setup.login(client, "mixb", "securepass1")
        feed = client.get("/api/feed").json()
        # Only the friends post should appear
        assert len(feed) == 1
        assert feed[0]["content"] == "friends post"

    def test_visibility_filtering_before_pagination(self, client):
        """When limit=1 and there are 1 visible + N invisible, still get visible."""
        a, b = _Setup.register_pair(client, "vis")
        _Setup.become_friends(client, a["id"], b["id"], "visa", "visb")
        _Setup.login(client, "visa", "securepass1")
        # ONLY_ME (invisible to B)
        client.post("/api/posts", json={"content": "invisible", "audience": "ONLY_ME"})
        # FRIENDS (visible to B)
        client.post("/api/posts", json={"content": "visible", "audience": "FRIENDS"})
        _Setup.login(client, "visb", "securepass1")
        feed = client.get("/api/feed?limit=1").json()
        assert len(feed) == 1
        assert feed[0]["content"] == "visible"


# ---------------------------------------------------------------------------
# AC7: Auth and error envelope for posts/feeds
# ---------------------------------------------------------------------------

class TestAC7AuthErrors:

    def test_anonymous_create_post_401(self, client):
        resp = client.post("/api/posts", json={"content": "x", "audience": "ONLY_ME"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_read_post_401(self, client):
        resp = client.get("/api/posts/999")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_feed_401(self, client):
        resp = client.get("/api/feed")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_delete_401(self, client):
        resp = client.delete("/api/posts/999")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_forbidden_edit_shape(self, client):
        a, b = _Setup.register_pair(client, "ac7f")
        _Setup.login(client, "ac7fa", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "locked", "audience": "FRIENDS",
        }).json()["id"]
        _Setup.login(client, "ac7fb", "securepass1")
        resp = client.patch(f"/api/posts/{post_id}", json={"content": "cracked"})
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        err = resp.json()
        assert "error" in err
        assert err["error"]["code"] == "forbidden"

    def test_unknown_post_not_found(self, client):
        _Setup.register(client, "ac7n", "ac7n@tt.com")
        _Setup.login(client, "ac7n", "securepass1")
        resp = client.get("/api/posts/99999")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_replayed_delete_corrupts_nothing(self, client):
        _Setup.register(client, "ac7r", "ac7r@tt.com")
        _Setup.login(client, "ac7r", "securepass1")
        post_id = client.post("/api/posts", json={
            "content": "delete me", "audience": "ONLY_ME",
        }).json()["id"]
        client.delete(f"/api/posts/{post_id}")
        # Replied delete → 404, not internal error
        resp = client.delete(f"/api/posts/{post_id}")
        assert resp.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# AC8: Unit test coverage already proven; integration proven by AC1-AC7 tests.
# The feed with equal-timestamp tiebreaker is verified via TestAC6FeedOrdering.
# ---------------------------------------------------------------------------

class TestPublicAudience:

    def test_public_post_visible_to_stranger(self, client):
        _author = _Setup.register(client, "public_author", "pub_author@tt.com")
        _stranger = _Setup.register(client, "public_stranger", "pub_stranger@tt.com")
        _Setup.login(client, "public_author", "securepass1")
        post_id = _Setup.create_post(client, audience="PUBLIC", content="Open to everyone")

        _Setup.login(client, "public_stranger", "securepass1")
        feed = client.get("/api/feed").json()
        assert any(p["id"] == post_id for p in feed)
        direct = client.get(f"/api/posts/{post_id}")
        assert direct.status_code == status.HTTP_200_OK
        assert direct.json()["audience"] == "PUBLIC"

    def test_public_post_hidden_from_blocked_viewer(self, client):
        author = _Setup.register(client, "public_author2", "pub_author2@tt.com")
        _stranger = _Setup.register(client, "public_stranger2", "pub_stranger2@tt.com")
        _Setup.login(client, "public_author2", "securepass1")
        post_id = _Setup.create_post(client, audience="PUBLIC", content="Open to everyone")

        # Stranger blocks the author — public visibility must collapse.
        _Setup.login(client, "public_stranger2", "securepass1")
        block = _Setup.block(client, "public_stranger2", author.json()["id"])
        assert block.status_code == status.HTTP_201_CREATED

        feed = client.get("/api/feed").json()
        assert not any(p["id"] == post_id for p in feed)
        assert client.get(f"/api/posts/{post_id}").status_code == status.HTTP_404_NOT_FOUND

    def test_public_update_audience_accepted(self, client):
        _Setup.register(client, "public_editor", "pub_editor@tt.com")
        _Setup.login(client, "public_editor", "securepass1")
        post_id = _Setup.create_post(client, audience="ONLY_ME")
        resp = client.patch(f"/api/posts/{post_id}", json={"audience": "PUBLIC"})
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["audience"] == "PUBLIC"
