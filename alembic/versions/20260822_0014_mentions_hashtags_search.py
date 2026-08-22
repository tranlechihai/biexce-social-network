"""Mentions, hashtags and native post search (T-026).

Revision ID: 20260822_0014
Revises: 20260822_0013
"""

from datetime import datetime, timezone
import re
import unicodedata

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

revision = "20260822_0014"
down_revision = "20260822_0013"
branch_labels = None
depends_on = None

_OLD_KINDS = ("follow", "like", "comment", "repost", "follow_request")
_NEW_KINDS = _OLD_KINDS + ("mention",)
_ACTIVITY_COLUMNS = (
    "id", "user_id", "actor_id", "kind", "post_id", "created_at", "read_at",
)
_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{3,30})(?![A-Za-z0-9_])"
)
_HASHTAG_RE = re.compile(r"(?<!\w)#([^\W_][\w]{0,63})", re.UNICODE)


def _activity_shape(kinds: tuple[str, ...]) -> sa.Table:
    meta = sa.MetaData()
    for stub in ("users", "posts"):
        sa.Table(stub, meta, sa.Column("id", sa.Integer(), primary_key=True))
    values = ", ".join(f"'{kind}'" for kind in kinds)
    return sa.Table(
        "activities_new",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"kind IN ({values})", name="ck_activity_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
    )


def _sqlite_rebuild_activities(kinds: tuple[str, ...]) -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS activities_new"))
    op.execute(CreateTable(_activity_shape(kinds)))
    columns = ", ".join(_ACTIVITY_COLUMNS)
    op.execute(sa.text(
        f"INSERT INTO activities_new ({columns}) SELECT {columns} FROM activities"
    ))
    op.execute(sa.text("DROP TABLE activities"))
    op.execute(sa.text("ALTER TABLE activities_new RENAME TO activities"))
    op.create_index("ix_activities_user_id", "activities", ["user_id"])


def _extract_unique(pattern, content: str, normalize, limit: int = 20) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for match in pattern.finditer(content):
        value = normalize(match.group(1))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
            if len(values) == limit:
                break
    return values


def _backfill_entities() -> None:
    bind = op.get_bind()
    users = {
        row.username: row.id
        for row in bind.execute(sa.text(
            "SELECT id, username FROM users "
            "WHERE banned_at IS NULL AND deactivated_at IS NULL"
        )).mappings()
    }
    posts = bind.execute(sa.text("SELECT id, content FROM posts ORDER BY id")).mappings()
    now = datetime.now(timezone.utc)
    mention_rows = []
    hashtag_rows = []
    for post in posts:
        names = _extract_unique(_MENTION_RE, post["content"], str.lower)
        for name in names:
            user_id = users.get(name)
            if user_id is not None:
                mention_rows.append({
                    "post_id": post["id"], "mentioned_user_id": user_id,
                    "created_at": now,
                })
        tags = _extract_unique(
            _HASHTAG_RE,
            post["content"],
            lambda value: unicodedata.normalize("NFKC", value).casefold()[:64],
        )
        hashtag_rows.extend({
            "post_id": post["id"], "tag": tag, "created_at": now,
        } for tag in tags)

    mentions = sa.table(
        "post_mentions",
        sa.column("post_id", sa.Integer()),
        sa.column("mentioned_user_id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    hashtags = sa.table(
        "post_hashtags",
        sa.column("post_id", sa.Integer()),
        sa.column("tag", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    if mention_rows:
        bind.execute(sa.insert(mentions), mention_rows)
    if hashtag_rows:
        bind.execute(sa.insert(hashtags), hashtag_rows)


def _sqlite_create_fts() -> None:
    op.execute(sa.text(
        "CREATE VIRTUAL TABLE posts_fts USING fts5("
        "content, content='posts', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 0')"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER posts_fts_ai AFTER INSERT ON posts BEGIN "
        "INSERT INTO posts_fts(rowid, content) VALUES (new.id, new.content); END"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER posts_fts_ad AFTER DELETE ON posts BEGIN "
        "INSERT INTO posts_fts(posts_fts, rowid, content) "
        "VALUES ('delete', old.id, old.content); END"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER posts_fts_au AFTER UPDATE OF content ON posts BEGIN "
        "INSERT INTO posts_fts(posts_fts, rowid, content) "
        "VALUES ('delete', old.id, old.content); "
        "INSERT INTO posts_fts(rowid, content) VALUES (new.id, new.content); END"
    ))
    op.execute(sa.text("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')"))


def _sqlite_drop_fts() -> None:
    for trigger in ("posts_fts_ai", "posts_fts_ad", "posts_fts_au"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
    op.execute(sa.text("DROP TABLE IF EXISTS posts_fts"))


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    op.create_table(
        "post_mentions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("mentioned_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mentioned_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("post_id", "mentioned_user_id", name="uq_post_mention"),
    )
    op.create_index("ix_post_mentions_post_id", "post_mentions", ["post_id"])
    op.create_index(
        "ix_post_mentions_mentioned_user_id", "post_mentions", ["mentioned_user_id"],
    )
    op.create_table(
        "post_hashtags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("tag", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("post_id", "tag", name="uq_post_hashtag"),
    )
    op.create_index("ix_post_hashtags_post_id", "post_hashtags", ["post_id"])
    op.create_index("ix_post_hashtags_tag_post", "post_hashtags", ["tag", "post_id"])
    _backfill_entities()

    if dialect == "postgresql":
        op.execute(sa.text("ALTER TABLE activities DROP CONSTRAINT ck_activity_kind"))
        op.execute(sa.text(
            "ALTER TABLE activities ADD CONSTRAINT ck_activity_kind "
            "CHECK (kind IN ('follow','like','comment','repost','follow_request','mention'))"
        ))
        op.execute(sa.text(
            "CREATE INDEX ix_posts_search_fts ON posts USING gin "
            "(to_tsvector('simple', coalesce(content, '')))"
        ))
    else:
        _sqlite_rebuild_activities(_NEW_KINDS)
        _sqlite_create_fts()


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    # Fail before ANY DDL. SQLite virtual-table DDL is not reliably rolled
    # back with a later CHECK failure; without this preflight a failed
    # downgrade could leave the 0014 stamp while silently removing FTS.
    mention_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM activities WHERE kind = 'mention'"
    )).scalar_one()
    if mention_count:
        raise ValueError(
            "Cannot downgrade 0014 while mention notification rows exist; "
            "remove/archive them first."
        )
    if dialect == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ix_posts_search_fts"))
        op.execute(sa.text("ALTER TABLE activities DROP CONSTRAINT ck_activity_kind"))
        op.execute(sa.text(
            "ALTER TABLE activities ADD CONSTRAINT ck_activity_kind "
            "CHECK (kind IN ('follow','like','comment','repost','follow_request'))"
        ))
    else:
        _sqlite_drop_fts()
        _sqlite_rebuild_activities(_OLD_KINDS)
    op.drop_index("ix_post_hashtags_tag_post", table_name="post_hashtags")
    op.drop_index("ix_post_hashtags_post_id", table_name="post_hashtags")
    op.drop_table("post_hashtags")
    op.drop_index("ix_post_mentions_mentioned_user_id", table_name="post_mentions")
    op.drop_index("ix_post_mentions_post_id", table_name="post_mentions")
    op.drop_table("post_mentions")
