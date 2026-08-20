"""Safety & moderation: reports table, generalized mutes, user moderation flags.

Changes:
* ``users.is_moderator`` (bool, NOT NULL default false) and
  ``users.banned_at`` (datetime, nullable) — ban lifecycle.
* ``mutes`` generalized to user mutes OR post mutes:
  * ``target_id`` becomes nullable (user mutes),
  * new ``post_id`` column (post mutes, ON DELETE CASCADE from posts),
  * ``uq_mute_pair`` replaced by partial unique indexes
    ``uq_mute_user`` (muted_by,target_id) and ``uq_mute_post`` (muted_by,post_id),
  * ``ck_mute_exactly_one_target`` enforces exactly one target flavor.
* ``hidden_posts`` dropped (post mutes live in ``mutes``).
* ``reports`` table — reporter, target user, optional post/comment anchor,
  reason + status enums, resolution audit (resolved_by, resolved_at, note).

Revision ID: 20260819_0005
Revises: 20260818_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "20260819_0005"
down_revision = "20260818_0004"
branch_labels = None
depends_on = None


def _idx(name, table, cols):
    return (name, table, cols)


def _recreate(table: str, columns: list, indexes: list, copy: tuple,
              where: str | None = None) -> None:
    """Manual 12-step table recreation (SQLite).

    ``indexes`` entries: ``(name, table, cols[, unique])``.
    """
    op.create_table(f"{table}_new", *columns)
    old_names, new_names = copy
    where_clause = f" WHERE {where}" if where else ""
    op.execute(
        f"INSERT INTO {table}_new ({', '.join(new_names)}) "
        f"SELECT {', '.join(old_names)} FROM {table}{where_clause}"
    )
    op.execute(f"DROP TABLE {table}")
    op.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    for spec in indexes:
        name, tbl, cols = spec[0], spec[1], spec[2]
        unique = spec[3] if len(spec) > 3 else False
        op.create_index(name, tbl, cols, unique=unique)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def _users_columns(with_moderation: bool) -> list:
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("bio", sa.String(), nullable=True),
    ]
    if with_moderation:
        columns.append(
            sa.Column("is_moderator", sa.Boolean(), nullable=False,
                      server_default=sa.text("0")),
        )
        columns.append(
            sa.Column("banned_at", sa.DateTime(), nullable=True),
        )
    return columns


def _users_indexes() -> list:
    return [
        ("ix_users_username", "users", ["username"], True),
        ("ix_users_email", "users", ["email"], True),
    ]


# ---------------------------------------------------------------------------
# mutes
# ---------------------------------------------------------------------------

def _mutes_columns(generalized: bool) -> list:
    if not generalized:
        return [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "muted_by", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=False,
            ),
            sa.Column(
                "target_id", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("muted_by", "target_id", name="uq_mute_pair"),
            sa.CheckConstraint("muted_by <> target_id", name="ck_mute_not_self"),
        ]
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "muted_by", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "target_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column(
            "post_id", sa.Integer(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("muted_by <> target_id", name="ck_mute_not_self"),
        sa.CheckConstraint(
            "(target_id IS NOT NULL) <> (post_id IS NOT NULL)",
            name="ck_mute_exactly_one_target",
        ),
    ]


def _mutes_indexes(generalized: bool) -> list:
    if not generalized:
        return [
            _idx("ix_mutes_muted_by", "mutes", ["muted_by"]),
            _idx("ix_mutes_target_id", "mutes", ["target_id"]),
        ]
    return [
        _idx("ix_mutes_muted_by", "mutes", ["muted_by"]),
        _idx("ix_mutes_target_id", "mutes", ["target_id"]),
        _idx("ix_mutes_post_id", "mutes", ["post_id"]),
    ]


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def _create_reports() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "reporter_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "target_user_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        # SET NULL: a report outlives the content it pinned (audit trail).
        sa.Column(
            "post_id", sa.Integer(),
            sa.ForeignKey("posts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "comment_id", sa.Integer(),
            sa.ForeignKey("comments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "resolved_by", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "reporter_id", "target_user_id", "post_id", "comment_id",
            name="uq_report_target",
        ),
        sa.CheckConstraint(
            "reason IN ('spam','harassment','hate_speech','false_info','other')",
            name="ck_report_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_report_status",
        ),
        )
    for col in ("reporter_id", "target_user_id", "post_id", "comment_id"):
        op.create_index(f"ix_reports_{col}", "reports", [col])


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    dialect = op.get_context().dialect.name

    # -- users ----------------------------------------------------------------
    if dialect == "postgresql":
        op.add_column(
            "users",
            sa.Column("is_moderator", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
        )
        op.add_column(
            "users",
            sa.Column("banned_at", sa.DateTime(), nullable=True),
        )
    else:
        _recreate(
            "users",
            _users_columns(with_moderation=True),
            _users_indexes(),
            copy=(
                ("id", "username", "email", "password_hash",
                 "display_name", "bio", "0", "NULL"),
                ("id", "username", "email", "password_hash",
                 "display_name", "bio", "is_moderator", "banned_at"),
            ),
        )

    # -- mutes ----------------------------------------------------------------
    if dialect == "postgresql":
        # Drop post-mute rows first is impossible — none exist yet.
        op.execute("ALTER TABLE mutes DROP CONSTRAINT IF EXISTS uq_mute_pair")
        op.execute("ALTER TABLE mutes ALTER COLUMN target_id DROP NOT NULL")
        op.add_column(
            "mutes",
            sa.Column("post_id", sa.Integer(), nullable=True),
        )
        op.execute(
            "ALTER TABLE mutes ADD CONSTRAINT fk_mutes_post "
            "FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE"
        )
        op.create_index("ix_mutes_post_id", "mutes", ["post_id"])
        op.create_index(
            "uq_mute_user", "mutes", ["muted_by", "target_id"],
            unique=True, postgresql_where=sa.text("target_id IS NOT NULL"),
        )
        op.create_index(
            "uq_mute_post", "mutes", ["muted_by", "post_id"],
            unique=True, postgresql_where=sa.text("post_id IS NOT NULL"),
        )
    else:
        # SQLite: fold hidden_posts rows into mutes as post mutes, recreate
        # mutes with the generalized shape (unique indexes added AFTER the
        # fold so it cannot collide), then drop hidden_posts.
        _recreate(
            "mutes",
            _mutes_columns(generalized=True),
            [],  # indexes added below after the fold
            copy=(
                ("id", "muted_by", "target_id", "created_at"),
                ("id", "muted_by", "target_id", "created_at"),
            ),
        )
        op.execute(
            "INSERT OR IGNORE INTO mutes (muted_by, target_id, post_id, created_at) "
            "SELECT user_id, NULL, post_id, created_at FROM hidden_posts"
        )
        op.drop_index("ix_hidden_posts_post_id", table_name="hidden_posts")
        op.drop_index("ix_hidden_posts_user_id", table_name="hidden_posts")
        op.drop_table("hidden_posts")
        op.create_index("ix_mutes_muted_by", "mutes", ["muted_by"])
        op.create_index("ix_mutes_target_id", "mutes", ["target_id"])
        op.create_index("ix_mutes_post_id", "mutes", ["post_id"])
        op.create_index(
            "uq_mute_user", "mutes", ["muted_by", "target_id"],
            unique=True, sqlite_where=sa.text("target_id IS NOT NULL"),
        )
        op.create_index(
            "uq_mute_post", "mutes", ["muted_by", "post_id"],
            unique=True, sqlite_where=sa.text("post_id IS NOT NULL"),
        )

    # -- reports ----------------------------------------------------------------
    _create_reports()


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    dialect = op.get_context().dialect.name

    op.drop_index("ix_reports_comment_id", table_name="reports")
    op.drop_index("ix_reports_post_id", table_name="reports")
    op.drop_index("ix_reports_target_user_id", table_name="reports")
    op.drop_index("ix_reports_reporter_id", table_name="reports")
    op.drop_table("reports")

    if dialect == "postgresql":
        op.execute("DELETE FROM mutes WHERE post_id IS NOT NULL")
        op.drop_index("uq_mute_post", table_name="mutes")
        op.drop_index("uq_mute_user", table_name="mutes")
        op.drop_index("ix_mutes_post_id", table_name="mutes")
        op.execute("ALTER TABLE mutes DROP CONSTRAINT IF EXISTS fk_mutes_post")
        op.drop_column("mutes", "post_id")
        op.execute("ALTER TABLE mutes ALTER COLUMN target_id SET NOT NULL")
        op.create_unique_constraint("uq_mute_pair", "mutes", ["muted_by", "target_id"])
        op.drop_column("users", "banned_at")
        op.drop_column("users", "is_moderator")
        return

    # Re-create hidden_posts from post mutes, then recreate mutes.
    op.create_table(
        "hidden_posts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "post_id", sa.Integer(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "post_id", name="uq_hidden_post"),
    )
    op.execute(
        "INSERT INTO hidden_posts (user_id, post_id, created_at) "
        "SELECT muted_by, post_id, created_at FROM mutes WHERE post_id IS NOT NULL"
    )
    _recreate(
        "mutes",
        _mutes_columns(generalized=False),
        [
            _idx("ix_mutes_muted_by", "mutes", ["muted_by"]),
            _idx("ix_mutes_target_id", "mutes", ["target_id"]),
        ],
        copy=(
            ("id", "muted_by", "target_id", "created_at"),
            ("id", "muted_by", "target_id", "created_at"),
        ),
        where="target_id IS NOT NULL",  # post mutes live in hidden_posts now
    )
    op.create_index("ix_hidden_posts_user_id", "hidden_posts", ["user_id"])
    op.create_index("ix_hidden_posts_post_id", "hidden_posts", ["post_id"])

    _recreate(
        "users",
        _users_columns(with_moderation=False),
        _users_indexes(),
        copy=(
            ("id", "username", "email", "password_hash", "display_name", "bio"),
            ("id", "username", "email", "password_hash", "display_name", "bio"),
        ),
    )