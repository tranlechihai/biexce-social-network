"""Privacy + follow approval (T-024).

Adds the account-privacy foundation for the follow-approval flow:

* ``users.is_private`` (BOOLEAN NOT NULL DEFAULT false) — when set, the
  user's ``PUBLIC`` posts are visible only to friends and ACTIVE followers
  (the ``FOLLOWERS`` audience is always follower-only).  Pending followers
  see nothing.
* ``follows.status`` (VARCHAR NOT NULL DEFAULT 'active',
  CHECK IN ('pending','active')) — following a PRIVATE account creates a
  ``pending`` row (a follow request); the target approves (``active``) or
  rejects (row deleted).  Rejection/unfollow/cancellation all delete the
  row — a follow edge that does not apply simply does not exist, which
  keeps ``uq_follow_pair`` and every count trivially correct.
  Pre-existing rows are backfilled ``active`` (they were auto-approved).
* ``activities`` kind domain gains ``follow_request`` (the notification a
  private account receives for an incoming follow).  On PostgreSQL the
  named CHECK is replaced in place; on SQLite the constraint is inline and
  the table is rebuilt (same 11-step pattern as 0010/0012).
* ``posts`` audience domain gains ``FOLLOWERS`` (author + active
  followers).  PostgreSQL replaces the named CHECK in place; SQLite rebuilds
  the table — children (likes, comments, media, ...) reference ``posts`` by
  name and the migration connection runs with foreign_keys OFF (house rule,
  T-022), so the drop/rename is safe and re-resolves cleanly.

Compatibility: code running against a pre-0013 database never sees
``follow_request`` rows or ``FOLLOWERS`` posts (the domains could not
contain them) and treats all follows as active — the ``status`` column's
default makes the two states indistinguishable until this revision lands on
BOTH sides.

Revision ID: 20260822_0013
Revises: 20260822_0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

revision = "20260822_0013"
down_revision = "20260822_0012"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Domain value lists (single source — both the shape builders and the PG
# in-place constraint swaps read from here so the directions cannot drift).
# ---------------------------------------------------------------------------

_ACTIVITY_KINDS_OLD = ("follow", "like", "comment", "repost")
_ACTIVITY_KINDS_NEW = _ACTIVITY_KINDS_OLD + ("follow_request",)

_AUDIENCES_OLD = ("ONLY_ME", "FRIENDS", "PUBLIC")
_AUDIENCES_NEW = _AUDIENCES_OLD + ("FOLLOWERS",)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_ACTIVITY_KINDS_NEW_SQL = _quoted(_ACTIVITY_KINDS_NEW)
_AUDIENCES_NEW_SQL = _quoted(_AUDIENCES_NEW)

# Same column list as the original 0001 create — the data copy never drifts.
_ACTIVITY_COLUMNS = (
    "id",
    "user_id",
    "actor_id",
    "kind",
    "post_id",
    "created_at",
    "read_at",
)


def _kinds_sql() -> str:
    return "kind IN (" + _ACTIVITY_KINDS_NEW_SQL + ")"


def _activities_shape(kinds: tuple[str, ...]) -> sa.Table:
    """The current ``activities`` shape as a Core table.

    ``kinds`` selects which kind domain the CHECK carries (the old list for
    the downgrade target, the extended list for the upgrade rebuild) so the
    two directions can never drift.
    """
    meta = sa.MetaData()
    # Stubs — only needed so CreateTable can resolve FK targets.
    for stub in ("users", "posts"):
        sa.Table(stub, meta, sa.Column("id", sa.Integer(), primary_key=True))

    check_sql = "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"
    return sa.Table(
        "activities_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(check_sql, name="ck_activity_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
    )


def _sqlite_rebuild_activities(kinds: tuple[str, ...]) -> None:
    # env.py runs each migration in one transaction (transactional DDL), so a
    # failure anywhere rolls the whole rebuild back.
    op.execute(sa.text("DROP TABLE IF EXISTS activities_new"))
    op.execute(CreateTable(_activities_shape(kinds)))
    op.execute(
        sa.text(
            f"INSERT INTO activities_new ({', '.join(_ACTIVITY_COLUMNS)}) "
            f"SELECT {', '.join(_ACTIVITY_COLUMNS)} FROM activities"
        )
    )
    op.execute(sa.text("DROP TABLE activities"))
    op.execute(sa.text("ALTER TABLE activities_new RENAME TO activities"))
    op.create_index("ix_activities_user_id", "activities", ["user_id"])


_POSTS_COLUMNS = (
    "id",
    "author_id",
    "content",
    "audience",
    "created_at",
    "updated_at",
)


def _posts_shape(audiences: tuple[str, ...]) -> sa.Table:
    """The current ``posts`` shape as a Core table (see dev-PG inspection):
    varchar content, two non-unique indexes, author FK ON DELETE CASCADE."""
    meta = sa.MetaData()
    sa.Table("users", meta, sa.Column("id", sa.Integer(), primary_key=True))
    check_sql = "audience IN (" + ", ".join(f"'{a}'" for a in audiences) + ")"
    return sa.Table(
        "posts_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(check_sql, name="ck_post_audience"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="CASCADE"),
    )


def _sqlite_rebuild_posts(audiences: tuple[str, ...]) -> None:
    # The migration connection runs with foreign_keys OFF (house rule — the
    # drop does not cascade into the child tables), and the children reference
    # ``posts`` BY NAME, so after the rename their DDL re-resolves to the new
    # table.  0010/0012 use the identical rebuild pattern.
    op.execute(sa.text("DROP TABLE IF EXISTS posts_new"))
    op.execute(CreateTable(_posts_shape(audiences)))
    op.execute(
        sa.text(
            f"INSERT INTO posts_new ({', '.join(_POSTS_COLUMNS)}) "
            f"SELECT {', '.join(_POSTS_COLUMNS)} FROM posts"
        )
    )
    op.execute(sa.text("DROP TABLE posts"))
    op.execute(sa.text("ALTER TABLE posts_new RENAME TO posts"))
    op.create_index("ix_posts_author_id", "posts", ["author_id"])
    op.create_index("ix_posts_created_at_id", "posts", ["created_at", "id"])


def upgrade() -> None:
    dialect = op.get_context().dialect.name

    # 1) users.is_private — plain ADD COLUMN on both dialects (constant
    #    server default; PG 11+ fills it without a table rewrite).
    op.execute(sa.text(
        "ALTER TABLE users ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT false"
    ))

    # 2) follows.status — ADD COLUMN with CHECK.  Both dialects accept a
    #    CHECK on ADD COLUMN when a NOT NULL column carries a default that
    #    satisfies it (SQLite rule); PG adds the named constraint separately.
    if dialect == "postgresql":
        op.execute(sa.text(
            "ALTER TABLE follows "
            "ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active', "
            "ADD CONSTRAINT ck_follow_status "
            "CHECK (status IN ('pending', 'active'))"
        ))
    else:
        op.execute(sa.text(
            "ALTER TABLE follows ADD COLUMN status VARCHAR(16) NOT NULL "
            "DEFAULT 'active' CHECK (status IN ('pending', 'active'))"
        ))

    # 3) activities kind domain — extend with 'follow_request'.
    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE activities DROP CONSTRAINT ck_activity_kind"))
        op.execute(sa.text(
            "ALTER TABLE activities ADD CONSTRAINT ck_activity_kind "
            f"CHECK ({_kinds_sql()})"
        ))
    else:
        _sqlite_rebuild_activities(_ACTIVITY_KINDS_NEW)

    # 4) posts audience domain — extend with 'FOLLOWERS'.
    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE posts DROP CONSTRAINT ck_post_audience"))
        op.execute(sa.text(
            "ALTER TABLE posts ADD CONSTRAINT ck_post_audience "
            f"CHECK (audience IN ({_AUDIENCES_NEW_SQL}))"
        ))
    else:
        _sqlite_rebuild_posts(_AUDIENCES_NEW)


def downgrade() -> None:
    dialect = op.get_context().dialect.name

    # Note: downgrading while 'follow_request' rows exist FAILS by design on
    # the rebuild/insert — the old domain cannot hold them and the migration
    # rolls back transactionally rather than silently dropping notifications.
    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE activities DROP CONSTRAINT ck_activity_kind"))
        op.execute(sa.text(
            "ALTER TABLE activities ADD CONSTRAINT ck_activity_kind "
            "CHECK (kind IN ('follow', 'like', 'comment', 'repost'))"
        ))
    else:
        _sqlite_rebuild_activities(_ACTIVITY_KINDS_OLD)

    # Downgrade the posts audience domain (and with it, any FOLLOWERS rows —
    # the rebuild's INSERT fails and the transaction rolls back, same
    # fail-closed posture as the follow_request rows above).
    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE posts DROP CONSTRAINT ck_post_audience"))
        op.execute(sa.text(
            "ALTER TABLE posts ADD CONSTRAINT ck_post_audience "
            f"CHECK (audience IN ({_quoted(_AUDIENCES_OLD)}))"
        ))
    else:
        _sqlite_rebuild_posts(_AUDIENCES_OLD)

    op.execute(sa.text("ALTER TABLE follows DROP COLUMN status"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN is_private"))