"""Account deletion — tombstones and evidence-survives-delete FKs (T-023).

Two changes:

* New ``deleted_accounts`` table — a 30-day tombstone that locks a deleted
  account's username and email. While the tombstone is fresh, registering with
  the same username/email is rejected; after the window the identifiers are
  reusable (the row may be purged by the T-030 jobs worker).
* Rebuild ``reports`` so the user references (``reporter_id``,
  ``target_user_id``, ``resolved_by``) are NULLABLE with ``ON DELETE SET NULL``.
  A moderation report is evidence/audit: it must outlive the deletion of the
  users it points at, so deleting an account anonymizes the report (NULL refs)
  instead of failing the FK or destroying the audit trail.

Revision ID: 20260821_0010
Revises: 20260821_0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

revision = "20260821_0010"
down_revision = "20260821_0009"
branch_labels = None
depends_on = None

# Reports columns are copied verbatim; only the nullability + ON DELETE of the
# three user references change. Kept in one list so upgrade/downgrade can never
# drift from the model.
_REPORT_COLUMNS = (
    "id",
    "reporter_id",
    "target_user_id",
    "post_id",
    "comment_id",
    "reason",
    "status",
    "resolution_note",
    "resolved_by",
    "created_at",
    "resolved_at",
)

_REPORT_INDEXES = [
    ("ix_reports_reporter_id", "reports", ["reporter_id"]),
    ("ix_reports_target_user_id", "reports", ["target_user_id"]),
    ("ix_reports_post_id", "reports", ["post_id"]),
    ("ix_reports_comment_id", "reports", ["comment_id"]),
]


def _new_deleted_accounts() -> sa.Table:
    meta = sa.MetaData()
    return sa.Table(
        "deleted_accounts",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username", name="uq_deleted_account_username"),
        sa.UniqueConstraint("email", name="uq_deleted_account_email"),
    )


def _rebuild_reports(user_fk_ondelete: str | None) -> sa.Table:
    """The full ``reports`` schema as a Core table.

    ``user_fk_ondelete`` is ``"SET NULL"`` when users are deletable (upgrade)
    and ``None`` on downgrade back to the legacy NOT-NULL / no-ondelete shape.
    Built once and reused for both directions so the check constraint and the
    unique pair rule can never drift between upgrade and downgrade.
    """
    meta = sa.MetaData()
    # Stubs — only needed so CreateTable can resolve FK targets when emitting
    # ``REFERENCES ...``.
    for stub in ("users", "posts", "comments"):
        sa.Table(stub, meta, sa.Column("id", sa.Integer(), primary_key=True))

    if user_fk_ondelete is None:
        # Legacy shape: reporter/target NOT NULL, resolved_by nullable; none of
        # the user FKs carry an ON DELETE rule (deleting a user was not
        # supported, so no cascade/SET NULL was ever needed).
        reporter = sa.Column("reporter_id", sa.Integer(), nullable=False)
        target = sa.Column("target_user_id", sa.Integer(), nullable=False)
        resolved_by = sa.Column("resolved_by", sa.Integer(), nullable=True)
        user_fks = [
            sa.ForeignKeyConstraint(["reporter_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        ]
    else:
        reporter = sa.Column("reporter_id", sa.Integer(), nullable=True)
        target = sa.Column("target_user_id", sa.Integer(), nullable=True)
        resolved_by = sa.Column("resolved_by", sa.Integer(), nullable=True)
        user_fks = [
            sa.ForeignKeyConstraint(
                ["reporter_id"], ["users.id"], ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["target_user_id"], ["users.id"], ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["resolved_by"], ["users.id"], ondelete="SET NULL",
            ),
        ]

    return sa.Table(
        "reports_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        reporter,
        target,
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        resolved_by,
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('spam','harassment','hate_speech','false_info','other')",
            name="ck_report_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_report_status",
        ),
        sa.UniqueConstraint(
            "reporter_id", "target_user_id", "post_id", "comment_id",
            name="uq_report_target",
        ),
        *user_fks,
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["comments.id"], ondelete="SET NULL",
        ),
    )


def _sqlite_rebuild_reports(user_fk_ondelete: str | None) -> None:
    # SQLite cannot ALTER a column's nullability or FK: rebuild and copy.
    # env.py runs each migration in one transaction (transactional DDL), so a
    # failure anywhere rolls the whole rebuild back.
    op.execute(sa.text("DROP TABLE IF EXISTS reports_new"))
    op.execute(CreateTable(_rebuild_reports(user_fk_ondelete)))
    op.execute(
        sa.text(
            f"INSERT INTO reports_new ({', '.join(_REPORT_COLUMNS)}) "
            f"SELECT {', '.join(_REPORT_COLUMNS)} FROM reports"
        )
    )
    op.execute(sa.text("DROP TABLE reports"))
    op.execute(sa.text("ALTER TABLE reports_new RENAME TO reports"))
    for name, tbl, cols in _REPORT_INDEXES:
        op.create_index(name, tbl, cols)


def upgrade() -> None:
    op.execute(CreateTable(_new_deleted_accounts()))
    _sqlite_rebuild_reports("SET NULL")


def downgrade() -> None:
    # Revert reports to the legacy NOT-NULL shape first (the tombstone table
    # has no FK to protect).
    _sqlite_rebuild_reports(None)
    op.drop_table("deleted_accounts")