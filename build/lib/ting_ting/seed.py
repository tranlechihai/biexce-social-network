"""Representative seed data — guarded, transactional, fresh-only.

Pre-flight checks:
1. Target must be SQLite/PostgreSQL and schema-compatible.
2. Target must be empty — ALL seed tables have zero rows.
3. Demo password must be provided via ``TING_DEMO_PASSWORD`` env var.
If any check fails the seed exits BEFORE any write with an actionable error
that does not leak data counts or secrets.

All data is written inside a single transaction so any later failure
rolls back the complete operation.
"""

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ting_ting.auth import hash_password
from ting_ting.models import (
    Comment, FriendRequest, Like, Post, User,
)


# ---------------------------------------------------------------------------
# Demo credentials — required at runtime; no fallback default
# ---------------------------------------------------------------------------

def _get_demo_password() -> str:
    """Read demo password from environment at runtime.

    Requires ``TING_DEMO_PASSWORD`` — exits if not set.  Never prints the
    value to stdout or stderr.
    """
    password = os.environ.get("TING_DEMO_PASSWORD")
    if not password:
        print(
            "ERROR: TING_DEMO_PASSWORD environment variable is required. "
            "Set it before seeding (e.g. export TING_DEMO_PASSWORD=yourpass).",
            file=sys.stderr,
        )
        sys.exit(1)
    return password


# Users to seed
_USERS = [
    {"username": "alice", "email": "alice@tingting.dev", "display_name": "Alice Dev", "bio": "Seed user A"},
    {"username": "bob", "email": "bob@tingting.dev", "display_name": "Bob Dev", "bio": "Seed user B"},
    {"username": "carol", "email": "carol@tingting.dev", "display_name": "Carol Dev", "bio": "Seed user C"},
]


# Every logical table checked for emptiness during preflight.
_SEED_TABLES = [
    "users",
    "friend_requests",
    "blocks",
    "posts",
    "likes",
    "comments",
]


def _preflight(engine) -> None:
    """Validate that the engine and target DB are safe for seeding.

    Raises ``SystemExit`` (code 1) with an actionable error on any violation.
    Refusal text reveals only the target selection and that no mutation
    occurred — never row counts or data summaries.
    """

    # 1) Must be a supported application database
    if engine.name not in {"sqlite", "postgresql"}:
        print(
            "ERROR: Database must be SQLite or PostgreSQL. "
            f"Got '{engine.name}'. Update TING_DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # 2) Required tables must exist
    missing_tables = set(_SEED_TABLES) - existing_tables
    if missing_tables:
        print(
            f"ERROR: Missing tables: {missing_tables!r}. "
            "Run schema initialization first (python -m ting_ting).",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3) Schema compatibility — users table columns
    required_cols = {"id", "username", "email", "password_hash", "display_name", "bio"}
    existing_cols = {col["name"] for col in inspector.get_columns("users")}
    missing_cols = required_cols - existing_cols
    if missing_cols:
        print(
            f"ERROR: Incompatible 'users' table — missing columns {missing_cols!r}. "
            "Will not seed an incompatible database.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4) ALL seed tables must be empty — check every logical table for rows
    with engine.connect() as conn:
        for table in _SEED_TABLES:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar()
            if count > 0:
                # Refusal text: target selection + no mutation, no data count,
                # no guidance to delete/remove external files
                print(
                    "ERROR: Target database already contains data. "
                    "Seed only writes to a fresh, empty, compatible "
                    "database. No mutation was performed.",
                    file=sys.stderr,
                )
                sys.exit(1)


def run(engine_or_url: str | None = None) -> None:
    """Execute the guarded seed.

    Parameters
    ----------
    engine_or_url : str | None
        Either a SQLAlchemy URL string or ``None`` to derive the engine from
        the standard ``TING_DATABASE_URL`` pattern.  When ``None`` is passed
        a temporary in-memory engine is NOT used — the configured database
        target is re-used so that the seed persists.
    """
    from sqlalchemy import select as sa_select

    if engine_or_url is not None:
        # Accept either a SQLAlchemy URL or a bare file path
        url = engine_or_url if "://" in engine_or_url else f"sqlite:///{engine_or_url}"
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        from ting_ting.database import get_engine
        engine = get_engine()

    # Preflight — exits on any failure (no writes)
    _preflight(engine)
    print("Preflight passed — schema compatible, database empty.")

    # Create a session factory for this engine
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Begin transaction
    session: Session = factory()
    try:
        demo_password = _get_demo_password()
        pw_hash = hash_password(demo_password)
        now = datetime.now(timezone.utc)

        # --- Users ---
        users = {}
        for u in _USERS:
            user = User(
                username=u["username"],
                email=u["email"],
                password_hash=pw_hash,
                display_name=u.get("display_name"),
                bio=u.get("bio"),
            )
            session.add(user)
        session.flush()
        for u in _USERS:
            user_obj = session.execute(
                sa_select(User).where(User.username == u["username"])
            ).scalar_one()
            users[u["username"]] = user_obj

        alice = users["alice"]
        bob = users["bob"]
        carol = users["carol"]

        # --- Friendship: alice <-> bob (accepted) ---
        left, right = (alice.id, bob.id) if alice.id < bob.id else (bob.id, alice.id)
        friendship = FriendRequest(
            sender_id=alice.id,
            recipient_id=bob.id,
            canonical_left=left,
            canonical_right=right,
            state="accepted",
            created_at=now,
        )
        session.add(friendship)
        session.flush()

        # --- Posts ---
        # Alice's FRIENDS post (visible to Bob)
        post_friends = Post(
            author_id=alice.id,
            content="Hello friends! Alice here and this is visible to my friends.",
            audience="FRIENDS",
            created_at=now,
            updated_at=now,
        )
        session.add(post_friends)

        # Alice's ONLY_ME post (visible only to Alice)
        post_only_me = Post(
            author_id=alice.id,
            content="This is my private thought, only I can read this.",
            audience="ONLY_ME",
            created_at=now,
            updated_at=now,
        )
        session.add(post_only_me)

        # Bob's FRIENDS post
        post_bob = Post(
            author_id=bob.id,
            content="Bob here! Testing posts for friends.",
            audience="FRIENDS",
            created_at=now,
            updated_at=now,
        )
        session.add(post_bob)

        session.flush()

        # --- Like: Bob likes Alice's FRIENDS post ---
        like = Like(user_id=bob.id, post_id=post_friends.id, created_at=now)
        session.add(like)

        # --- Comment: Bob comments on Alice's FRIENDS post ---
        comment = Comment(
            post_id=post_friends.id,
            author_id=bob.id,
            content="Nice post, Alice!",
            created_at=now,
        )
        session.add(comment)

        # --- Carol is isolated (friends with nobody) ---
        post_carol = Post(
            author_id=carol.id,
            content="Carol here, isolated but posting.",
            audience="FRIENDS",
            created_at=now,
            updated_at=now,
        )
        session.add(post_carol)

        # Commit everything in one transaction
        session.commit()

        print("Seed completed successfully.")
        print("  Users: alice, bob, carol (password set via TING_DEMO_PASSWORD)")
        print("  Friendship: alice <-> bob")
        print("  Posts: alice (FRIENDS + ONLY_ME), bob (FRIENDS), carol (FRIENDS)")
        print("  Like: bob -> alice's FRIENDS post")
        print("  Comment: bob on alice's FRIENDS post")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """CLI: ``python -m ting_ting.seed``"""
    run()


if __name__ == "__main__":
    main()
