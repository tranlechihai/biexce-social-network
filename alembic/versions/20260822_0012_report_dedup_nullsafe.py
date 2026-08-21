"""NULL-safe report dedup: functional unique index (T-015E).

The 4-column unique constraint ``uq_report_target`` cannot dedupe rows whose
``comment_id`` is NULL — SQL treats NULLs as distinct — so two racing
concurrent inserts of the same post report (``post_id`` set, ``comment_id``
NULL) both succeed and duplicate the moderation queue.

Replace it with the functional unique index ``ux_reports_dedup``:

    (reporter_id, target_user_id, post_id, coalesce(comment_id, 0))

COALESCE applies to ``comment_id`` ONLY, never to ``post_id``: when a
moderator deletes a post, the FK ``ON DELETE SET NULL`` turns an anchored
report into ``(reporter, target, NULL, 0)``.  A fully COALESCEd index would
make that row collide with an already-existing bare account report
``(reporter, target, NULL->0, 0)`` and permanently block the deletion of
that post.  A raw ``post_id`` (NULL != 0) can never collide.

Accepted trade-off: racing *bare account* reports (``post_id`` NULL) are
still not deduped at the database level.  Such rows carry no content
anchor, are low-frequency, and are trivially filterable in the queue.

SQLite keeps the legacy UNIQUE as an inline table constraint (autoindex),
which cannot be dropped without a table rebuild; PostgreSQL only needs
``DROP CONSTRAINT`` + ``CREATE UNIQUE INDEX``.

Revision ID: 20260822_0012
Revises: 20260821_0011
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

revision = "20260822_0012"
down_revision = "20260821_0011"
branch_labels = None
depends_on = None

# Same column list as 0010's rebuild — the data copy below never drifts.
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

_DEDUP_INDEX_DDL = (
    "CREATE UNIQUE INDEX ux_reports_dedup ON reports "
    "(reporter_id, target_user_id, post_id, coalesce(comment_id, 0))"
)


def _reports_shape(with_legacy_uq: bool) -> sa.Table:
    """The current ``reports`` shape (post-0010) as a Core table.

    ``with_legacy_uq`` selects the 4-column UNIQUE for the downgrade target
    and the bare shape for the upgrade rebuild.  Built once per direction so
    the check constraints and FK actions can never drift.
    """
    meta = sa.MetaData()
    # Stubs — only needed so CreateTable can resolve FK targets when emitting
    # ``REFERENCES ...``.
    for stub in ("users", "posts", "comments"):
        sa.Table(stub, meta, sa.Column("id", sa.Integer(), primary_key=True))

    constraints = [
        sa.CheckConstraint(
            "reason IN ('spam','harassment','hate_speech','false_info','other')",
            name="ck_report_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_report_status",
        ),
    ]
    if with_legacy_uq:
        constraints.append(
            sa.UniqueConstraint(
                "reporter_id", "target_user_id", "post_id", "comment_id",
                name="uq_report_target",
            )
        )

    return sa.Table(
        "reports_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reporter_id", sa.Integer(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        *constraints,
        sa.ForeignKeyConstraint(
            ["reporter_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["comments.id"], ondelete="SET NULL",
        ),
    )


def _sqlite_rebuild_reports(with_legacy_uq: bool) -> None:
    # env.py runs each migration in one transaction (transactional DDL), so a
    # failure anywhere rolls the whole rebuild back.
    op.execute(sa.text("DROP TABLE IF EXISTS reports_new"))
    op.execute(CreateTable(_reports_shape(with_legacy_uq)))
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


def _preexisting_duplicates(connection) -> int:
    """Duplicate groups the new index would reject: more than one row per
    (reporter, target, post_id, coalesce(comment_id, 0)).

    Only rows fully non-NULL in the RAW columns can violate the index (a
    UNIQUE index never fires when any indexed value is NULL — and
    GROUP BY, unlike the index, treats NULLs as equal), so rows touched by
    an ON DELETE SET NULL (null reporter/target/post) are excluded.
    """
    row = connection.execute(sa.text(
        "SELECT COUNT(*) FROM ("
        "  SELECT 1 FROM reports"
        "  WHERE reporter_id IS NOT NULL"
        "  AND target_user_id IS NOT NULL"
        "  AND post_id IS NOT NULL"
        "  GROUP BY reporter_id, target_user_id, post_id,"
        "           coalesce(comment_id, 0)"
        "  HAVING COUNT(*) > 1"
        ") d"
    )).one()
    return row[0]


def upgrade() -> None:
    # Fail closed on pre-existing racing duplicates: a database that managed
    # to insert two identical post reports before this revision cannot create
    # the dedup index.  No silent dedup — an operator keeps one row per
    # (reporter_id, target_user_id, post_id, coalesce(comment_id, 0)) and
    # re-runs.  Mirrors 0007's fail-closed orphan handling.  This check runs
    # before any mutation, so a refusal leaves the database untouched.
    duplicates = _preexisting_duplicates(op.get_context().connection)
    if duplicates:
        raise ValueError(
            f"0012 cannot create ux_reports_dedup: {duplicates} duplicate "
            "report group(s) already exist. Keep one row per "
            "(reporter_id, target_user_id, post_id, coalesce(comment_id, 0)) "
            "and re-run the migration."
        )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        # The 0010 rebuild recreated uq_report_target as a named constraint;
        # dropping it also drops the backing index (PG index names are unique
        # per schema, so it must be gone before the new one is created).
        op.execute(sa.text(
            "ALTER TABLE reports DROP CONSTRAINT IF EXISTS uq_report_target"
        ))
    else:
        _sqlite_rebuild_reports(with_legacy_uq=False)

    op.execute(sa.text(_DEDUP_INDEX_DDL))


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ux_reports_dedup"))
        op.execute(sa.text(
            "ALTER TABLE reports ADD CONSTRAINT uq_report_target UNIQUE "
            "(reporter_id, target_user_id, post_id, comment_id)"
        ))
    else:
        op.execute(sa.text("DROP INDEX ux_reports_dedup"))
        _sqlite_rebuild_reports(with_legacy_uq=True)