"""Rotating refresh tokens.

Adds ``refresh_tokens`` — opaque rotating refresh credentials bound to the
server-side ``sessions`` table (T-021). The access JWT stays short-lived; a
refresh token lets a still-active session mint new access tokens after the
JWT expires. Only token hashes are stored; every use rotates the row and
re-play of a rotated token kills its session.

Revision ID: 20260820_0008
Revises: 20260819_0007
"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_0008"
down_revision = "20260819_0007"
branch_labels = None
depends_on = None


def _create_refresh_tokens() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id", sa.String(length=32),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("replaced_by_id", sa.Integer(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def upgrade() -> None:
    _create_refresh_tokens()


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")