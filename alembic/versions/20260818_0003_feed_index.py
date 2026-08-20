"""Feed ordering index.

Revision ID: 20260818_0003
Revises: 20260818_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_0003"
down_revision = "20260818_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_posts_created_at_id", "posts", ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_posts_created_at_id", table_name="posts")