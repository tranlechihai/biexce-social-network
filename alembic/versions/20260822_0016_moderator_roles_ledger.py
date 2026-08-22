"""Moderator roles, warnings, enriched bans and action ledger (T-028).

Revision ID: 20260822_0016
Revises: 20260822_0015
"""

from alembic import op
import sqlalchemy as sa


revision = "20260822_0016"
down_revision = "20260822_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(16), nullable=True, server_default="user"),
    )
    op.add_column("users", sa.Column("banned_until", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("ban_reason", sa.String(120), nullable=True))
    op.add_column("users", sa.Column("banned_by", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE users SET role = CASE WHEN is_moderator THEN 'moderator' ELSE 'user' END"
    )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "role", existing_type=sa.String(16), nullable=False,
            server_default="user",
        )
        batch.create_check_constraint(
            "ck_users_role", "role IN ('user', 'moderator', 'admin')",
        )
        batch.create_foreign_key(
            "fk_users_banned_by_users", "users", ["banned_by"], ["id"],
            ondelete="SET NULL",
        )
        batch.drop_column("is_moderator")

    op.create_table(
        "user_warnings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("issued_by", sa.Integer(), nullable=True),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 120", name="ck_warning_reason",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issued_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_warnings_user_id", "user_warnings", ["user_id"])

    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(16), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("previous_state", sa.String(32), nullable=True),
        sa.Column("new_state", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "action_type IN ('warning_issued','user_banned','user_unbanned',"
            "'report_resolved','report_dismissed','post_removed',"
            "'comment_removed','role_changed')",
            name="ck_moderation_action_type",
        ),
        sa.CheckConstraint(
            "resource_type IS NULL OR resource_type IN "
            "('user','report','post','comment')",
            name="ck_moderation_action_resource_type",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_moderation_actions_actor_id", "moderation_actions", ["actor_id"],
    )
    op.create_index(
        "ix_moderation_actions_target_user_id",
        "moderation_actions",
        ["target_user_id"],
    )
    op.create_index(
        "ix_moderation_actions_created_id",
        "moderation_actions",
        ["created_at", "id"],
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
            CREATE TRIGGER trg_moderation_actions_no_update
            BEFORE UPDATE ON moderation_actions
            WHEN NOT (
                (NEW.actor_id IS OLD.actor_id OR (
                    NEW.actor_id IS NULL AND OLD.actor_id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM users WHERE id = OLD.actor_id)
                ))
                AND (NEW.target_user_id IS OLD.target_user_id OR (
                    NEW.target_user_id IS NULL AND OLD.target_user_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM users WHERE id = OLD.target_user_id
                    )
                ))
                AND NEW.action_type IS OLD.action_type
                AND NEW.reason IS OLD.reason
                AND NEW.note IS OLD.note
                AND NEW.resource_type IS OLD.resource_type
                AND NEW.resource_id IS OLD.resource_id
                AND NEW.previous_state IS OLD.previous_state
                AND NEW.new_state IS OLD.new_state
                AND NEW.created_at IS OLD.created_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'moderation_action_immutable');
            END
        """)
        op.execute("""
            CREATE TRIGGER trg_moderation_actions_no_delete
            BEFORE DELETE ON moderation_actions
            BEGIN
                SELECT RAISE(ABORT, 'moderation_action_immutable');
            END
        """)
    else:
        op.execute("""
            CREATE FUNCTION protect_moderation_actions() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'moderation_action_immutable';
                END IF;
                IF (
                    (NEW.actor_id IS NOT DISTINCT FROM OLD.actor_id OR (
                        NEW.actor_id IS NULL AND OLD.actor_id IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM users WHERE id = OLD.actor_id)
                    ))
                    AND (NEW.target_user_id IS NOT DISTINCT FROM OLD.target_user_id OR (
                        NEW.target_user_id IS NULL AND OLD.target_user_id IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM users WHERE id = OLD.target_user_id
                        )
                    ))
                    AND NEW.action_type IS NOT DISTINCT FROM OLD.action_type
                    AND NEW.reason IS NOT DISTINCT FROM OLD.reason
                    AND NEW.note IS NOT DISTINCT FROM OLD.note
                    AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
                    AND NEW.resource_id IS NOT DISTINCT FROM OLD.resource_id
                    AND NEW.previous_state IS NOT DISTINCT FROM OLD.previous_state
                    AND NEW.new_state IS NOT DISTINCT FROM OLD.new_state
                    AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
                ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'moderation_action_immutable';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER trg_moderation_actions_immutable
            BEFORE UPDATE OR DELETE ON moderation_actions
            FOR EACH ROW EXECUTE FUNCTION protect_moderation_actions()
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_moderation_actions_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_moderation_actions_no_update")
    else:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_moderation_actions_immutable "
            "ON moderation_actions"
        )
        op.execute("DROP FUNCTION IF EXISTS protect_moderation_actions()")
    op.drop_index("ix_moderation_actions_created_id", table_name="moderation_actions")
    op.drop_index(
        "ix_moderation_actions_target_user_id", table_name="moderation_actions",
    )
    op.drop_index("ix_moderation_actions_actor_id", table_name="moderation_actions")
    op.drop_table("moderation_actions")
    op.drop_index("ix_user_warnings_user_id", table_name="user_warnings")
    op.drop_table("user_warnings")

    op.add_column(
        "users",
        sa.Column("is_moderator", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.execute(
        "UPDATE users SET is_moderator = CASE "
        "WHEN role IN ('moderator', 'admin') THEN true ELSE false END"
    )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "is_moderator", existing_type=sa.Boolean(), nullable=False,
            server_default=sa.false(),
        )
        batch.drop_constraint("fk_users_banned_by_users", type_="foreignkey")
        batch.drop_constraint("ck_users_role", type_="check")
        batch.drop_column("banned_by")
        batch.drop_column("ban_reason")
        batch.drop_column("banned_until")
        batch.drop_column("role")
