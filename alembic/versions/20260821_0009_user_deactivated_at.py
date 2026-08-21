"""Self-deactivation flag for account lifecycle (T-023).

Adds ``users.deactivated_at`` — a reversible, user-initiated pause. Unlike a
ban it does NOT block sign-in; it hides the account from other users' feeds,
search, public profiles, and graphs until reactivated.

Revision ID: 20260821_0009
Revises: 20260820_0008
"""

from alembic import op
import sqlalchemy as sa


revision = "20260821_0009"
down_revision = "20260820_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deactivated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deactivated_at")