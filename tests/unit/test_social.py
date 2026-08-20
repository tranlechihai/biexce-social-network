"""Unit tests for social graph rules — AC8 (pair canonicalization, transitions).

Cover canonicalization, block checks, and state transitions at the service layer.
"""

import pytest

from ting_ting.social import canonical_pair


# ---------------------------------------------------------------------------
# Canonical pair
# ---------------------------------------------------------------------------

class TestCanonicalPair:
    def test_ordered_output(self):
        assert canonical_pair(5, 2) == (2, 5)

    def test_equal_ids(self):
        assert canonical_pair(3, 3) == (3, 3)

    def test_already_ordered(self):
        assert canonical_pair(1, 10) == (1, 10)

    def test_large_ids(self):
        assert canonical_pair(99999, 1) == (1, 99999)


# ---------------------------------------------------------------------------
# Social service integration (uses DB for realistic checks)
# ---------------------------------------------------------------------------

@pytest.fixture
def social_users(tmp_session):
    """Two users ready for social graph testing."""
    from ting_ting.auth import hash_password
    from ting_ting.models import User

    a = User(username="socialA", email="a@st.com", password_hash=hash_password("pass1234"))
    b = User(username="socialB", email="b@st.com", password_hash=hash_password("pass1234"))
    tmp_session.add_all([a, b])
    tmp_session.commit()
    tmp_session.refresh(a)
    tmp_session.refresh(b)
    return {"a": a, "b": b}


class TestCreateRequest:
    def test_creates_pending(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request
        a, b = social_users["a"], social_users["b"]
        req = create_friend_request(tmp_session, a, b)
        assert req.state == "pending"
        assert req.sender_id == a.id
        assert req.recipient_id == b.id

    def test_self_request_rejected(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request
        a = social_users["a"]
        with pytest.raises(ValueError, match="self_request"):
            create_friend_request(tmp_session, a, a)

    def test_duplicate_request_rejected(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request
        a, b = social_users["a"], social_users["b"]
        create_friend_request(tmp_session, a, b)
        with pytest.raises(ValueError, match="already_exists"):
            create_friend_request(tmp_session, a, b)

    def test_reverse_duplicate_rejected(self, tmp_session, social_users):
        """Reverse direction (B→A) is rejected if A→B already exists."""
        from ting_ting.social import create_friend_request
        a, b = social_users["a"], social_users["b"]
        create_friend_request(tmp_session, a, b)
        with pytest.raises(ValueError, match="already_exists"):
            create_friend_request(tmp_session, b, a)


class TestAcceptReject:
    def _make_request(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request
        a, b = social_users["a"], social_users["b"]
        return create_friend_request(tmp_session, a, b)

    def test_accept_sets_accepted(self, tmp_session, social_users):
        from ting_ting.social import accept_friend_request
        req = self._make_request(tmp_session, social_users)
        b = social_users["b"]
        updated = accept_friend_request(tmp_session, req, b)
        assert updated.state == "accepted"

    def test_accept_by_non_recipient_rejected(self, tmp_session, social_users):
        from ting_ting.social import accept_friend_request
        req = self._make_request(tmp_session, social_users)
        a = social_users["a"]  # sender cannot accept own request
        with pytest.raises(ValueError, match="not_recipient"):
            accept_friend_request(tmp_session, req, a)

    def test_reject_sets_rejected(self, tmp_session, social_users):
        from ting_ting.social import reject_friend_request
        req = self._make_request(tmp_session, social_users)
        b = social_users["b"]
        updated = reject_friend_request(tmp_session, req, b)
        assert updated.state == "rejected"

    def test_accept_after_reject_rejected(self, tmp_session, social_users):
        from ting_ting.social import reject_friend_request, accept_friend_request
        req = self._make_request(tmp_session, social_users)
        b = social_users["b"]
        reject_friend_request(tmp_session, req, b)
        with pytest.raises(ValueError, match="not_pending"):
            accept_friend_request(tmp_session, req, b)

    def test_accept_after_block_rejected(self, tmp_session, social_users):
        from ting_ting.social import (
            block_user, create_friend_request, accept_friend_request,
        )
        a, b = social_users["a"], social_users["b"]
        req = create_friend_request(tmp_session, a, b)
        # Sender blocks the recipient before the recipient accepts.
        block_user(tmp_session, b, a)
        with pytest.raises(ValueError, match="blocked"):
            accept_friend_request(tmp_session, req, b)


class TestUnfriend:
    def _make_friendship(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request, accept_friend_request
        a, b = social_users["a"], social_users["b"]
        req = create_friend_request(tmp_session, a, b)
        return accept_friend_request(tmp_session, req, b)

    def test_unfriend_rejects_both(self, tmp_session, social_users):
        from ting_ting.social import unfriend
        self._make_friendship(tmp_session, social_users)
        a = social_users["a"]
        unfriend(tmp_session, a.id, social_users["b"].id, a)
        from ting_ting.social import relationship_state
        assert relationship_state(tmp_session, a.id, social_users["b"].id) == "none"

    def test_unfriend_not_participant_rejected(self, tmp_session, social_users):
        from ting_ting.social import unfriend
        self._make_friendship(tmp_session, social_users)
        # Create a third user
        from ting_ting.auth import hash_password
        from ting_ting.models import User
        c = User(username="socialC", email="c@st.com", password_hash=hash_password("pass"))
        tmp_session.add(c)
        tmp_session.commit()
        with pytest.raises(ValueError, match="not_participant"):
            unfriend(tmp_session, social_users["a"].id, social_users["b"].id, c)


class TestConstraintRepair:
    """Regression tests for runtime compatibility repair (D1).

    The old uq_pair_state constraint included 'rejected' in the unique
    triple, causing UNIQUE violations on repeated reject → new-request →
    reject and unfriend → refriend → unfriend flows on databases that have
    that legacy constraint.  The ix_active_pair partial index (in the model)
    fixes this for new databases.  For existing databases, the runtime logic
    in social.py deletes stale rejected rows before transitioning, making it
    safe under both constraint types without any schema migration.
    """

    def _prepare(self, tmp_session, social_users):
        from ting_ting.social import (
            create_friend_request,
            accept_friend_request,
            reject_friend_request,
            unfriend,
        )
        return social_users["a"], social_users["b"], (
            create_friend_request,
            accept_friend_request,
            reject_friend_request,
            unfriend,
        )

    def test_reject_new_request_reject_succeeds(self, tmp_session, social_users):
        """Reject → new request → reject must not raise IntegrityError."""
        a, b, (create_req, _, reject, _) = self._prepare(
            tmp_session, social_users
        )
        # Reject 1
        req1 = create_req(tmp_session, a, b)
        reject(tmp_session, req1, b)
        assert req1.state == "rejected"

        # New request 2
        req2 = create_req(tmp_session, a, b)
        assert req2.id != req1.id
        assert req2.state == "pending"

        # Reject 2 — must succeed (was UNIQUE violation before fix)
        reject(tmp_session, req2, b)
        assert req2.state == "rejected"

        # Stale rejected rows are cleaned up at transition time, so only the
        # latest rejected row remains for the pair.
        from sqlalchemy import text
        count = tmp_session.execute(
            text("SELECT COUNT(*) FROM friend_requests WHERE state='rejected'")
        ).scalar()
        assert count == 1

        # Relationship shows "none"
        from ting_ting.social import relationship_state
        assert relationship_state(tmp_session, a.id, b.id) == "none"

    def test_unfriend_refriend_unfriend_succeeds(self, tmp_session, social_users):
        """Unfriend → refriend → unfriend must not raise IntegrityError."""
        a, b, (create_req, accept, _, unfriend) = self._prepare(
            tmp_session, social_users
        )
        # Friendship 1 → unfriend
        req1 = create_req(tmp_session, a, b)
        accept(tmp_session, req1, b)
        assert req1.state == "accepted"
        unfriend(tmp_session, a.id, b.id, a)

        from ting_ting.social import relationship_state
        assert relationship_state(tmp_session, a.id, b.id) == "none"

        # Friendship 2 → unfriend (must succeed, was UNIQUE violation)
        req2 = create_req(tmp_session, a, b)
        assert req2.id != req1.id
        accept(tmp_session, req2, b)
        assert req2.state == "accepted"

        unfriend(tmp_session, a.id, b.id, b)
        # Must reach here without IntegrityError

        assert relationship_state(tmp_session, a.id, b.id) == "none"


class TestBlock:
    def test_block_removes_friendship(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request, accept_friend_request, block_user
        a, b = social_users["a"], social_users["b"]
        req = create_friend_request(tmp_session, a, b)
        accept_friend_request(tmp_session, req, b)
        from ting_ting.social import relationship_state
        assert relationship_state(tmp_session, a.id, b.id) == "friends"

        block_user(tmp_session, a, b)
        tmp_session.commit()

        # Friendship removed
        assert relationship_state(tmp_session, a.id, b.id) == "blocked_by_me"
        assert relationship_state(tmp_session, b.id, a.id) == "blocked_by_them"

    def test_blocked_pair_cannot_request(self, tmp_session, social_users):
        from ting_ting.social import block_user, create_friend_request
        a, b = social_users["a"], social_users["b"]
        block_user(tmp_session, a, b)
        tmp_session.commit()

        with pytest.raises(ValueError, match="blocked"):
            create_friend_request(tmp_session, a, b)
        with pytest.raises(ValueError, match="blocked"):
            create_friend_request(tmp_session, b, a)

    def test_self_block_rejected(self, tmp_session, social_users):
        from ting_ting.social import block_user
        a = social_users["a"]
        with pytest.raises(ValueError, match="self_block"):
            block_user(tmp_session, a, a)

    def test_unblock_does_not_restore(self, tmp_session, social_users):
        from ting_ting.social import create_friend_request, accept_friend_request, block_user, unblock_user, relationship_state
        a, b = social_users["a"], social_users["b"]
        req = create_friend_request(tmp_session, a, b)
        accept_friend_request(tmp_session, req, b)
        block_user(tmp_session, a, b)
        tmp_session.commit()

        # Unblock
        unblock_user(tmp_session, a, b.id)
        tmp_session.commit()

        # Friendship is NOT restored
        assert relationship_state(tmp_session, a.id, b.id) == "none"
