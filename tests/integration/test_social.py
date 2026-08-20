"""Integration tests for friend requests, friendships, block/unblock."""

from fastapi import status


class _Setup:

    @staticmethod
    def register(client, username, email, password="securepass1"):
        return client.post("/api/auth/register", json={
            "username": username, "email": email, "password": password,
        })

    @staticmethod
    def login(client, identifier, password):
        return client.post("/api/auth/login", json={
            "identifier": identifier, "password": password,
        })

    @staticmethod
    def register_pair(client):
        a = _Setup.register(client, "alice_2", "alice2@tt.com")
        assert a.status_code == status.HTTP_201_CREATED
        b = _Setup.register(client, "bob_2", "bob2@tt.com")
        assert b.status_code == status.HTTP_201_CREATED
        return a.json(), b.json()


class TestAC1FriendRequest:

    def test_send_request_creates_pending(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        resp = client.post("/api/social/requests", json={"target_user_id": b["id"]})
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["state"] == "pending"
        assert data["sender"]["id"] == a["id"]
        assert data["recipient"]["id"] == b["id"]
        assert "password" not in data
        assert "password_hash" not in str(data)

    def test_list_requests_shows_pending(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        resp = client.get("/api/social/requests")
        items = resp.json()
        pending = [i for i in items if i["state"] == "pending"]
        assert len(pending) == 1


class TestAC2RequestConflicts:

    def test_self_request_rejected(self, client):
        _Setup.register(client, "selfuser", "self@tt.com")
        _Setup.login(client, "selfuser", "securepass1")
        me = client.get("/api/profile/me").json()
        resp = client.post("/api/social/requests", json={"target_user_id": me["id"]})
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_duplicate_request_rejected(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        resp = client.post("/api/social/requests", json={"target_user_id": b["id"]})
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_reverse_duplicate_rejected(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        _Setup.login(client, "bob_2", "securepass1")
        resp = client.post("/api/social/requests", json={"target_user_id": a["id"]})
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_cannot_request_after_accepting(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        _Setup.login(client, "alice_2", "securepass1")
        resp = client.post("/api/social/requests", json={"target_user_id": b["id"]})
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_no_duplicate_row_on_conflict(self, client, tmp_engine):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        from sqlalchemy import text
        with tmp_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM friend_requests WHERE state = 'pending'")
            ).scalar()
        assert count == 1


class TestAC3Accept:

    def _send_id(self, client, target_id):
        _Setup.login(client, "alice_2", "securepass1")
        return client.post("/api/social/requests", json={"target_user_id": target_id}).json()["id"]

    def test_recipient_accepts(self, client):
        a, b = _Setup.register_pair(client)
        req_id = self._send_id(client, b["id"])
        _Setup.login(client, "bob_2", "securepass1")
        resp = client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["state"] == "accepted"

    def test_both_see_friends(self, client):
        a, b = _Setup.register_pair(client)
        req_id = self._send_id(client, b["id"])
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        _Setup.login(client, "alice_2", "securepass1")
        assert client.get(f"/api/social/relationship/{b['id']}").json()["state"] == "friends"
        _Setup.login(client, "bob_2", "securepass1")
        assert client.get(f"/api/social/relationship/{a['id']}").json()["state"] == "friends"

    def test_sender_cannot_accept(self, client):
        a, b = _Setup.register_pair(client)
        req_id = self._send_id(client, b["id"])
        _Setup.login(client, "alice_2", "securepass1")
        resp = client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_third_party_cannot_accept(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.register(client, "third_user", "third@tt.com")
        req_id = self._send_id(client, b["id"])
        _Setup.login(client, "third_user", "securepass1")
        resp = client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_replay_accept_after_accepted(self, client):
        a, b = _Setup.register_pair(client)
        req_id = self._send_id(client, b["id"])
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        resp2 = client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert resp2.status_code == status.HTTP_409_CONFLICT


class TestAC4RejectUnfriend:

    def test_recipient_rejects(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        resp = client.post("/api/social/requests/reject", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["state"] == "rejected"

    def test_reject_creates_no_friendship(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/reject", json={"request_id": req_id})
        rel = client.get(f"/api/social/relationship/{a['id']}").json()
        assert rel["state"] == "none"

    def test_sender_cannot_reject(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        resp = client.post("/api/social/requests/reject", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_third_party_cannot_reject(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.register(client, "third_user", "third@tt.com")
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "third_user", "securepass1")
        resp = client.post("/api/social/requests/reject", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_unfriend_removes_mutual(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        resp = client.post("/api/social/friends/unfriend", json={"target_user_id": a["id"]})
        assert resp.status_code == status.HTTP_200_OK
        assert client.get(f"/api/social/relationship/{a['id']}").json()["state"] == "none"
        _Setup.login(client, "alice_2", "securepass1")
        assert client.get(f"/api/social/relationship/{b['id']}").json()["state"] == "none"

    def test_replay_unfriend_silent(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        client.post("/api/social/friends/unfriend", json={"target_user_id": a["id"]})
        resp2 = client.post("/api/social/friends/unfriend", json={"target_user_id": a["id"]})
        assert resp2.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT)

    def test_reject_re_request_reject_no_crash(self, client):
        """Reject → new request → reject must not 500 (constraint repair).

        Regression for D1: old uq_pair_state constraint included 'rejected',
        so a second rejection created a UNIQUE violation and returned 500.
        After the repair, partial index ix_active_pair only covers
        pending/accepted states, so repeated rejections are safe.
        """
        a, b = _Setup.register_pair(client)
        # Round 1: request → reject
        _Setup.login(client, "alice_2", "securepass1")
        req1_id = client.post(
            "/api/social/requests", json={"target_user_id": b["id"]}
        ).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        resp = client.post(
            "/api/social/requests/reject", json={"request_id": req1_id}
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["state"] == "rejected"

        # Round 2: new request → reject (must succeed, not 500)
        _Setup.login(client, "alice_2", "securepass1")
        req2_id = client.post(
            "/api/social/requests", json={"target_user_id": b["id"]}
        ).json()["id"]
        assert req2_id != req1_id  # distinct row
        _Setup.login(client, "bob_2", "securepass1")
        resp2 = client.post(
            "/api/social/requests/reject", json={"request_id": req2_id}
        )
        assert resp2.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {resp2.status_code} — constraint may be broken"
        )
        assert resp2.json()["state"] == "rejected"

        # Relationship shows "none"
        rel = client.get(f"/api/social/relationship/{a['id']}").json()
        assert rel["state"] == "none"

    def test_unfriend_refriend_unfriend_no_crash(self, client):
        """Unfriend → new friendship → unfriend must not 500 (constraint repair).

        Mirror of reject-re-request-reject for the friendship path.
        """
        a, b = _Setup.register_pair(client)
        # Round 1: request → accept → unfriend
        _Setup.login(client, "alice_2", "securepass1")
        req1_id = client.post(
            "/api/social/requests", json={"target_user_id": b["id"]}
        ).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post(
            "/api/social/requests/accept", json={"request_id": req1_id}
        )
        assert client.get(
            f"/api/social/relationship/{a['id']}"
        ).json()["state"] == "friends"
        client.post(
            "/api/social/friends/unfriend", json={"target_user_id": a["id"]}
        )
        assert client.get(
            f"/api/social/relationship/{a['id']}"
        ).json()["state"] == "none"

        # Round 2: new request → accept → unfriend (must succeed, not 500)
        _Setup.login(client, "alice_2", "securepass1")
        req2_id = client.post(
            "/api/social/requests", json={"target_user_id": b["id"]}
        ).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post(
            "/api/social/requests/accept", json={"request_id": req2_id}
        )
        assert client.get(
            f"/api/social/relationship/{a['id']}"
        ).json()["state"] == "friends"
        resp2 = client.post(
            "/api/social/friends/unfriend", json={"target_user_id": a["id"]}
        )
        assert resp2.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {resp2.status_code} — constraint may be broken"
        )
        assert client.get(
            f"/api/social/relationship/{a['id']}"
        ).json()["state"] == "none"


class TestAC5Block:

    def _make_friends(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        return a, b

    def test_block_removes_friendship(self, client):
        a, b = self._make_friends(client)
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        assert client.get(f"/api/social/relationship/{a['id']}").json()["state"] == "blocked_by_me"
        _Setup.login(client, "alice_2", "securepass1")
        assert client.get(f"/api/social/relationship/{b['id']}").json()["state"] == "blocked_by_them"

    def test_block_removes_pending(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        client.post("/api/social/requests", json={"target_user_id": b["id"]})
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        assert client.get(f"/api/social/relationship/{a['id']}").json()["state"] == "blocked_by_me"

    def test_blocked_cannot_send_request(self, client):
        a, b = self._make_friends(client)
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        assert client.post("/api/social/requests", json={"target_user_id": a["id"]}).status_code == status.HTTP_409_CONFLICT
        _Setup.login(client, "alice_2", "securepass1")
        assert client.post("/api/social/requests", json={"target_user_id": b["id"]}).status_code == status.HTTP_409_CONFLICT

    def test_friends_list_excludes_blocked(self, client):
        a, b = self._make_friends(client)
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        friends = client.get("/api/social/friends").json()
        assert not any(f["id"] == a["id"] for f in friends)


class TestAC6Unblock:

    def test_unblock_restores_nothing(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        client.delete(f"/api/social/blocks/{a['id']}")
        assert client.get(f"/api/social/relationship/{a['id']}").json()["state"] == "none"

    def test_after_unblock_normal_flow(self, client):
        """New request possible only through normal flow."""
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/requests/accept", json={"request_id": req_id})
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        client.delete(f"/api/social/blocks/{a['id']}")
        _Setup.login(client, "bob_2", "securepass1")
        resp = client.post("/api/social/requests", json={"target_user_id": a["id"]})
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["state"] == "pending"

    def test_non_blocker_cannot_unblock(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.login(client, "bob_2", "securepass1")
        client.post("/api/social/blocks", json={"target_user_id": a["id"]})
        _Setup.login(client, "alice_2", "securepass1")
        resp = client.delete(f"/api/social/blocks/{b['id']}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


class TestAC7AuthErrors:

    def test_anonymous_requests_401(self, client):
        assert client.get("/api/social/requests").status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_friends_401(self, client):
        assert client.get("/api/social/friends").status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_blocks_401(self, client):
        assert client.get("/api/social/blocks").status_code == status.HTTP_401_UNAUTHORIZED

    def test_anonymous_post_request_401(self, client):
        assert client.post("/api/social/requests", json={"target_user_id": 1}).status_code == status.HTTP_401_UNAUTHORIZED

    def test_nonexistent_user_404(self, client):
        _Setup.register(client, "requser", "req@tt.com")
        _Setup.login(client, "requser", "securepass1")
        resp = client.post("/api/social/requests", json={"target_user_id": 99999})
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_nonexistent_request_accept_404(self, client):
        _Setup.register(client, "requser2", "req2@tt.com")
        _Setup.login(client, "requser2", "securepass1")
        resp = client.post("/api/social/requests/accept", json={"request_id": 99999})
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_unauthorized_accept_third_party(self, client):
        a, b = _Setup.register_pair(client)
        _Setup.register(client, "third_user", "third@tt.com")
        _Setup.login(client, "alice_2", "securepass1")
        req_id = client.post("/api/social/requests", json={"target_user_id": b["id"]}).json()["id"]
        _Setup.login(client, "third_user", "securepass1")
        resp = client.post("/api/social/requests/accept", json={"request_id": req_id})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_conflict_error_shape(self, client):
        """Self-request returns consistent conflict shape."""
        _Setup.register(client, "erruser", "err@tt.com")
        _Setup.login(client, "erruser", "securepass1")
        me = client.get("/api/profile/me").json()
        resp = client.post("/api/social/requests", json={"target_user_id": me["id"]})
        assert resp.status_code == status.HTTP_409_CONFLICT
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == "conflict"
