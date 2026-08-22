"""Notification preferences, aggregation indexes and unread dedup (T-027).

Revision ID: 20260822_0015
Revises: 20260822_0014
"""

from alembic import op
import sqlalchemy as sa

revision = "20260822_0015"
down_revision = "20260822_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("follow_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("follow_request_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("like_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("comment_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("repost_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mention_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column("activities", sa.Column("source_key", sa.String(96), nullable=True))
    op.create_index(
        "ux_activities_unread_dedup",
        "activities",
        ["user_id", "actor_id", "kind", "source_key"],
        unique=True,
        sqlite_where=sa.text("read_at IS NULL AND source_key IS NOT NULL"),
        postgresql_where=sa.text("read_at IS NULL AND source_key IS NOT NULL"),
    )
    op.create_index(
        "ix_activities_user_created_id",
        "activities",
        ["user_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_activities_user_created_id", table_name="activities")
    op.drop_index("ux_activities_unread_dedup", table_name="activities")
    op.drop_column("activities", "source_key")
    op.drop_table("notification_preferences")
