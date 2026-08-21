"""PostgreSQL parity corrections after the SQLite-first migration chain.

The 0001-0010 chain was developed and tested on SQLite; the PostgreSQL
branches carried three gaps that only surface on a real PostgreSQL database:

* 0005's PostgreSQL path generalized ``mutes`` (nullable ``target_id``,
  ``post_id`` column) but never added the
  ``ck_mute_exactly_one_target`` check constraint, and never dropped the
  legacy ``hidden_posts`` table (the SQLite branch does both).
* 0010 rebuilt ``reports`` via ``CREATE TABLE``/rename, which dropped the
  ``status`` server default ``'pending'``.
* Table rebuilds (0002, 0010) recreate sequences from 1 on PostgreSQL. A
  database that is populated and then migrated would collide with copied IDs
  on the next insert — the sequences must be re-anchored past ``MAX(id)``.

This revision fixes all three on PostgreSQL. On SQLite (the tested, in-use
backend) it is a deliberate no-op: the mute constraint and hidden_posts
cleanup already exist there, and SQLite has no sequences. The SQLite no-op
keeps both backends stamped at the same head, which the app requires.

Revision ID: 20260821_0011
Revises: 20260821_0010
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "20260821_0011"
down_revision = "20260821_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect != "postgresql":
        return  # SQLite is already at the intended shape (see docstring).

    connection = op.get_context().connection

    # Fail closed if the 0010 rebuild produced PKs without sequences —
    # integer PRIMARY KEY without SERIAL means inserts without an explicit
    # id can never work, and silently "passing" here would hide it.
    for table in ("reports", "deleted_accounts"):
        sequence = connection.execute(
            text(f"SELECT pg_get_serial_sequence('{table}', 'id')")
        ).scalar()
        if not sequence:
            raise RuntimeError(
                f"'{table}.id' has no backing sequence; the 0010 CREATE TABLE "
                "did not emit SERIAL. Fix the migration before upgrading."
            )

    # 1. Restore the constraint the 0005 PostgreSQL branch skipped.
    connection.execute(text(
        "ALTER TABLE mutes ADD CONSTRAINT ck_mute_exactly_one_target "
        "CHECK ((target_id IS NOT NULL) <> (post_id IS NOT NULL))"
    ))

    # 2. Drop the legacy table (empty on any migration-built database; the
    #    app has used mutes for post hiding since 0005).
    connection.execute(text("DROP TABLE IF EXISTS hidden_posts"))

    # 3. Restore the reports.status default dropped by the 0010 rebuild.
    connection.execute(text(
        "ALTER TABLE reports ALTER COLUMN status SET DEFAULT 'pending'"
    ))

    # 4. Re-anchor every application id sequence past its current MAX(id).
    #    Only column 'id' with a nextval default is considered, which skips
    #    alembic_version (its version_num is text, not an id sequence).
    rows = connection.execute(text(
        "SELECT table_name, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND column_name = 'id' "
        "AND column_default LIKE 'nextval(%'"
    )).fetchall()
    for table_name, default in rows:
        # default is shaped like nextval('public.users_id_seq'::regclass)
        sequence = default.split("'")[1]
        connection.execute(text(
            f"SELECT setval('{sequence}', "
            f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), "
            f"(SELECT MAX(id) FROM {table_name}) IS NOT NULL)"
        ))


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect != "postgresql":
        return

    connection = op.get_context().connection

    connection.execute(text(
        "ALTER TABLE mutes DROP CONSTRAINT IF EXISTS ck_mute_exactly_one_target"
    ))
    # Recreate the legacy table in its original empty shape (0004 form).
    # Note: this is a rehearsal-grade downgrade; the PostgreSQL downgrade
    # chain below 0005 is not a supported production path.
    connection.execute(
        sa.create_table(
            sa.Table(
                "hidden_posts",
                sa.MetaData(),
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
        )
    )
    connection.execute(text(
        "ALTER TABLE reports ALTER COLUMN status DROP DEFAULT"
    ))
    # Intentionally NOT resetting sequences: a downgrade is a rehearsal step,
    # and any data added after the upgrade would again need re-anchoring by
    # the operator. Downgrading to below 0005 is not supported (0005's
    # PostgreSQL branch was never a production path).