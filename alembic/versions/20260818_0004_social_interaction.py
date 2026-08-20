"""Comment replies + mutes + hidden posts.

Adds:
* ``comments.parent_comment_id`` (nullable, self-FK, ON DELETE CASCADE) —
  one level of comment nesting.
* ``mutes`` table — directional user mutes (no relationship effect).
* ``hidden_posts`` table — viewer-side post suppression for feeds.

Revision ID: 20260818_0004
Revises: 20260818_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_0004"
down_revision = "20260818_0003"
branch_labels = None
depends_on = None


def _idx(name, table, cols):
    return (name, table, cols)


def _recreate(
    table: str,
    columns: list,
    indexes: list,
    copy: tuple,
) -> None:
    """Manual 12-step table recreation (SQLite)."""
    op.create_table(f"{table}_new", *columns)
    old_names, new_names = copy
    op.execute(
        f"INSERT INTO {table}_new ({', '.join(new_names)}) "
        f"SELECT {', '.join(old_names)} FROM {table}"
    )
    op.execute(f"DROP TABLE {table}")
    op.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    for name, tbl, cols in indexes:
        op.create_index(name, tbl, cols)


def _create_mutes() -> None:
    op.create_table(
        "mutes",
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
    )
    op.create_index("ix_mutes_muted_by", "mutes", ["muted_by"])
    op.create_index("ix_mutes_target_id", "mutes", ["target_id"])


def _create_hidden_posts() -> None:
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
    op.create_index("ix_hidden_posts_user_id", "hidden_posts", ["user_id"])
    op.create_index("ix_hidden_posts_post_id", "hidden_posts", ["post_id"])


def _comments_columns(parent: bool) -> list:
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "post_id", sa.Integer(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "author_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    ]
    if parent:
        columns.insert(
            3,
            sa.Column(
                "parent_comment_id", sa.Integer(),
                sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=True,
            ),
        )
    return columns


def _comments_indexes() -> list:
    return [
        _idx("ix_comments_post_id", "comments", ["post_id"]),
        _idx("ix_comments_author_id", "comments", ["author_id"]),
        _idx("ix_comments_parent_comment_id", "comments", ["parent_comment_id"]),
    ]


def upgrade() -> None:
    dialect = op.get_context().dialect.name

    if dialect == "postgresql":
        op.add_column(
            "comments",
            sa.Column("parent_comment_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_comments_parent_comment_id", "comments", ["parent_comment_id"],
        )
        op.execute(
            "ALTER TABLE comments ADD CONSTRAINT fk_comment_parent "
            "FOREIGN KEY (parent_comment_id) REFERENCES comments (id) "
            "ON DELETE CASCADE"
        )
    else:
        # SQLite: recreate comments with the nullable parent column.
        _recreate(
            "comments",
            _comments_columns(parent=True),
            _comments_indexes(),
            copy=(
                ("id", "post_id", "author_id", "NULL",
                 "content", "created_at"),
                ("id", "post_id", "author_id", "parent_comment_id",
                 "content", "created_at"),
            ),
        )

    _create_mutes()
    _create_hidden_posts()


def downgrade() -> None:
    dialect = op.get_context().dialect.name

    op.drop_index("ix_hidden_posts_post_id", table_name="hidden_posts")
    op.drop_index("ix_hidden_posts_user_id", table_name="hidden_posts")
    op.drop_table("hidden_posts")
    op.drop_index("ix_mutes_target_id", table_name="mutes")
    op.drop_index("ix_mutes_muted_by", table_name="mutes")
    op.drop_table("mutes")

    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE comments DROP CONSTRAINT IF EXISTS fk_comment_parent"
        )
        op.drop_index("ix_comments_parent_comment_id", table_name="comments")
        op.drop_column("comments", "parent_comment_id")
        return

    _recreate(
        "comments",
        _comments_columns(parent=False),
        [
            _idx("ix_comments_post_id", "comments", ["post_id"]),
            _idx("ix_comments_author_id", "comments", ["author_id"]),
        ],
        copy=(
            ("id", "post_id", "author_id", "content", "created_at"),
            ("id", "post_id", "author_id", "content", "created_at"),
        ),
    )