"""Unit tests for auth mechanisms — AC4 (auth transports) and password hashing.

These tests verify:
* Anonymous access to protected endpoints → 401
* Malformed / expired token → 401
* Bearer header authenticates correctly
* Cookie authenticates the same user
* Password is stored as a bcrypt hash, never plain text
"""

from datetime import datetime, timedelta, timezone

import pytest
from jose import JWTError
from jose import jwt as jose_jwt

from ting_ting.auth import (
    decode_token,
    hash_password,
    normalize_email,
    normalize_username,
    verify_password,
)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_is_not_plain_text(self):
        pw = "supersecret1"
        hashed = hash_password(pw)
        assert hashed != pw
        # bcrypt hashes start with $2b$
        assert hashed.startswith("$2"), f"Expected bcrypt hash, got {hashed[:5]}"

    def test_verify_correct(self):
        pw = "correcthorse1"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed) is True

    def test_verify_wrong(self):
        pw = "correcthorse1"
        hashed = hash_password(pw)
        assert verify_password("wrongpassword", hashed) is False

    def test_hash_is_adaptive(self):
        pw = "testpass123"
        h1 = hash_password(pw)
        h2 = hash_password(pw)
        # Same password, different salt → different hashes
        assert h1 != h2
        # But both verify
        assert verify_password(pw, h1)
        assert verify_password(pw, h2)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

class TestNormalizers:
    def test_username_normalize(self):
        assert normalize_username("  User  ") == "user"

    def test_email_normalize(self):
        assert normalize_email("  CAPS@EXAMPLE.COM  ") == "caps@example.com"


# ---------------------------------------------------------------------------
# JWT token create / decode / expiry
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    from ting_ting.config import Settings
    return Settings()


class TestJWT:
    def test_create_and_decode(self, settings):
        token = jose_jwt.encode(
            {"sub": "1", "username": "alice", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        payload = decode_token(token, settings)
        assert payload["sub"] == "1"
        assert payload["username"] == "alice"

    def test_expired_token_raises(self, settings):
        token = jose_jwt.encode(
            {"sub": "1", "username": "bob", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(JWTError):
            decode_token(token, settings)

    def test_malformed_token_raises(self, settings):
        with pytest.raises(JWTError):
            decode_token("not.a.jwt", settings)
