"""Posts cascade on user delete.

``posts.author_id`` had no ON DELETE rule, so deleting a user (moderation /
data cleanup) left orphan posts and broke the profile render (author looked
up by username, orphan row showed up under the next user).

Revision ID: 20260819_0007
Revises: 20260819_0006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

revision = "20260819_0007"
down_revision = "20260819_0006"
branch_labels = None
depends_on = None

_NAMES = ("id", "author_id", "content", "audience", "created_at", "updated_at")
_INDEXES = [
    ("ix_posts_author_id", "posts", ["author_id"]),
    ("ix_posts_created_at_id", "posts", ["created_at", "id"]),
]


def _rebuild_table(ondelete: str | None) -> sa.Table:
    """The full ``posts`` schema as a Core table.

    Built once and reused for both directions so the check constraint and
    column types can never drift between upgrade and downgrade.
    """
    meta = sa.MetaData()
    # ``users`` stub — only needed so CreateTable can resolve the FK target
    # when emitting ``REFERENCES users(id)``.
    sa.Table(
        "users", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    return sa.Table(
        "posts_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "audience IN ('ONLY_ME', 'FRIENDS', 'PUBLIC')",
            name="ck_post_audience",
        ),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete=ondelete),
    )


def _sqlite_orphan_preflight(phase: str) -> None:
    """Fail closed before any DDL if ``posts`` references a missing user.

    Upgrading would carry broken rows into a schema whose strict FK the app
    now enforces; downgrading means a user was deleted after the upgrade
    (its posts already cascaded), so the legacy schema would be left
    knowingly inconsistent.  Either way: stop with a clear, actionable error
    before anything is touched.
    """
    bind = op.get_bind()
    orphans = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM posts "
            "WHERE author_id NOT IN (SELECT id FROM users)"
        )
    ).scalar()
    if orphans:
        raise ValueError(
            f"{phase}: {orphans} post row(s) reference missing user id(s); "
            "reassign or delete them, then retry"
        )


def _sqlite_rebuild(phase: str, ondelete: str | None) -> None:
    # SQLite cannot ALTER a foreign key, so the table is rebuilt and the rows
    # copied over.  env.py runs each migration file inside one real
    # transaction (transactional DDL), so a failure anywhere rolls the entire
    # rebuild back and the database stays unchanged.
    _sqlite_orphan_preflight(phase)
    # Clear a stale ``posts_new`` left by a tooling issue (safe here: the
    # original ``posts`` still holds all the data at this point).
    op.execute(sa.text("DROP TABLE IF EXISTS posts_new"))
    op.execute(CreateTable(_rebuild_table(ondelete)))
    op.execute(
        sa.text(
            f"INSERT INTO posts_new ({', '.join(_NAMES)}) "
            f"SELECT {', '.join(_NAMES)} FROM posts"
        )
    )
    op.execute(sa.text("DROP TABLE posts"))
    op.execute(sa.text("ALTER TABLE posts_new RENAME TO posts"))
    for name, tbl, cols in _INDEXES:
        op.create_index(name, tbl, cols)


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE posts "
            "DROP CONSTRAINT IF EXISTS posts_author_id_fkey"
        )
        op.execute(
            "ALTER TABLE posts ADD CONSTRAINT posts_author_id_fkey "
            "FOREIGN KEY (author_id) REFERENCES users (id) ON DELETE CASCADE"
        )
    else:
        # SQLite cannot ALTER a foreign key: rebuild the table and copy.
        _sqlite_rebuild("upgrade to 20260819_0007", "CASCADE")


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE posts DROP CONSTRAINT IF EXISTS posts_author_id_fkey"
        )
        op.execute(
            "ALTER TABLE posts ADD CONSTRAINT posts_author_id_fkey "
            "FOREIGN KEY (author_id) REFERENCES users (id)"
        )
    else:
        _sqlite_rebuild("downgrade from 20260819_0007", None)