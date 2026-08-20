"""Auth session registry.

Adds ``sessions`` — the server-side session table that makes logout and
password-change revocation real (a short-lived JWT carries ``sid``; a request
is valid only while its session row is unrevoked and unexpired).

Revision ID: 20260819_0006
Revises: 20260819_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20260819_0006"
down_revision = "20260819_0005"
branch_labels = None
depends_on = None


def _create_sessions() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def upgrade() -> None:
    _create_sessions()


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")