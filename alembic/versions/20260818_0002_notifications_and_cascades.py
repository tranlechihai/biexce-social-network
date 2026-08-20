"""Notifications read-state + cascade deletes + integrity checks.

Revision ID: 20260818_0002
Revises: 20260818_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_0002"
down_revision = "20260818_0001"
branch_labels = None
depends_on = None


_POST_FKS = {
    "likes": "likes_post_id_fkey",
    "comments": "comments_post_id_fkey",
    "saved_posts": "saved_posts_post_id_fkey",
    "reposts": "reposts_post_id_fkey",
    "post_media": "post_media_post_id_fkey",
    "activities": "activities_post_id_fkey",
}


def _idx(name, table, cols, unique=False, where=None):
    kwargs = {"unique": unique}
    if where is not None:
        kwargs["sqlite_where"] = sa.text(where)
        kwargs["postgresql_where"] = sa.text(where)
    return (name, table, list(cols), kwargs)


def _recreate(
    table: str,
    columns: list,
    indexes: list,
    copy: tuple | None,
) -> None:
    """Manual 12-step table recreation (SQLite).

    ``copy`` is ``(old_names, new_names)`` or ``None`` to skip the data copy.
    """
    op.create_table(f"{table}_new", *columns)
    if copy is not None:
        old_names, new_names = copy
        op.execute(
            f"INSERT INTO {table}_new ({', '.join(new_names)}) "
            f"SELECT {', '.join(old_names)} FROM {table}"
        )
    op.execute(f"DROP TABLE {table}")
    op.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    for name, tbl, cols, kwargs in indexes:
        op.create_index(name, tbl, cols, **kwargs)


def _user_fk(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), sa.ForeignKey("users.id"), nullable=False)


def _post_fk_cascade() -> sa.Column:
    return sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)


def _friend_request_columns(with_checks: bool) -> list:
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        _user_fk("sender_id"),
        _user_fk("recipient_id"),
        sa.Column("canonical_left", sa.Integer(), nullable=False),
        sa.Column("canonical_right", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    ]
    if with_checks:
        columns += [
            sa.CheckConstraint(
                "state IN ('pending','accepted','rejected')",
                name="ck_friend_request_state",
            ),
            sa.CheckConstraint("sender_id <> recipient_id", name="ck_friend_request_not_self"),
            sa.CheckConstraint("canonical_left < canonical_right", name="ck_friend_request_canonical"),
        ]
    return columns


def _friend_request_indexes() -> list:
    return [
        _idx("ix_friend_requests_sender_id", "friend_requests", ["sender_id"]),
        _idx("ix_friend_requests_recipient_id", "friend_requests", ["recipient_id"]),
        _idx("ix_friend_requests_canonical_left", "friend_requests", ["canonical_left"]),
        _idx(
            "ix_active_pair",
            "friend_requests",
            ["canonical_left", "canonical_right"],
            unique=True,
            where="state IN ('pending', 'accepted')",
        ),
    ]


def upgrade() -> None:
    dialect = op.get_context().dialect.name

    if dialect == "postgresql":
        op.add_column("activities", sa.Column("read_at", sa.DateTime(), nullable=True))
        op.execute(
            "ALTER TABLE activities ADD CONSTRAINT ck_activity_kind "
            "CHECK (kind IN ('follow','like','comment','repost'))"
        )
        op.execute(
            "ALTER TABLE follows ADD CONSTRAINT ck_follow_not_self "
            "CHECK (follower_id <> followed_id)"
        )
        op.execute(
            "ALTER TABLE posts ADD CONSTRAINT ck_post_audience "
            "CHECK (audience IN ('ONLY_ME', 'FRIENDS', 'PUBLIC'))"
        )
        for table, fk_name in _POST_FKS.items():
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} "
                f"FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE"
            )
        _recreate(
            "friend_requests",
            _friend_request_columns(with_checks=True),
            _friend_request_indexes(),
            copy=(
                ("id", "sender_id", "recipient_id", "canonical_left",
                 "canonical_right", "state", "created_at"),
                ("id", "sender_id", "recipient_id", "canonical_left",
                 "canonical_right", "state", "created_at"),
            ),
        )
        return

    # ------------------------------------------------------------------ SQLite
    _recreate(
        "activities",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("user_id"),
            _user_fk("actor_id"),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "kind IN ('follow','like','comment','repost')",
                name="ck_activity_kind",
            ),
        ],
        [_idx("ix_activities_user_id", "activities", ["user_id"])],
        copy=(
            ("id", "user_id", "actor_id", "kind", "post_id", "created_at"),
            ("id", "user_id", "actor_id", "kind", "post_id", "created_at"),
        ),
    )

    _recreate(
        "follows",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("follower_id"),
            _user_fk("followed_id"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("follower_id", "followed_id", name="uq_follow_pair"),
            sa.CheckConstraint("follower_id <> followed_id", name="ck_follow_not_self"),
        ],
        [
            _idx("ix_follows_follower_id", "follows", ["follower_id"]),
            _idx("ix_follows_followed_id", "follows", ["followed_id"]),
        ],
        copy=(
            ("id", "follower_id", "followed_id", "created_at"),
            ("id", "follower_id", "followed_id", "created_at"),
        ),
    )

    _recreate(
        "posts",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("author_id"),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("audience", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "audience IN ('ONLY_ME', 'FRIENDS', 'PUBLIC')",
                name="ck_post_audience",
            ),
        ],
        [_idx("ix_posts_author_id", "posts", ["author_id"])],
        copy=(
            ("id", "author_id", "content", "audience", "created_at", "updated_at"),
            ("id", "author_id", "content", "audience", "created_at", "updated_at"),
        ),
    )

    for table, unique_name in (("saved_posts", "uq_saved_post"), ("reposts", "uq_repost")):
        _recreate(
            table,
            [
                sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                _user_fk("user_id"),
                _post_fk_cascade(),
                sa.Column("created_at", sa.DateTime(), nullable=False),
                sa.UniqueConstraint("user_id", "post_id", name=unique_name),
            ],
            [
                _idx(f"ix_{table}_user_id", table, ["user_id"]),
                _idx(f"ix_{table}_post_id", table, ["post_id"]),
            ],
            copy=(
                ("id", "user_id", "post_id", "created_at"),
                ("id", "user_id", "post_id", "created_at"),
            ),
        )

    _recreate(
        "likes",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("user_id"),
            _post_fk_cascade(),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "post_id", name="uq_user_post_like"),
        ],
        [
            _idx("ix_likes_user_id", "likes", ["user_id"]),
            _idx("ix_likes_post_id", "likes", ["post_id"]),
        ],
        copy=(
            ("id", "user_id", "post_id", "created_at"),
            ("id", "user_id", "post_id", "created_at"),
        ),
    )

    _recreate(
        "comments",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _post_fk_cascade(),
            _user_fk("author_id"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        [
            _idx("ix_comments_post_id", "comments", ["post_id"]),
            _idx("ix_comments_author_id", "comments", ["author_id"]),
        ],
        copy=(
            ("id", "post_id", "author_id", "content", "created_at"),
            ("id", "post_id", "author_id", "content", "created_at"),
        ),
    )

    _recreate(
        "post_media",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _post_fk_cascade(),
            sa.Column("path", sa.String(), nullable=False),
            sa.Column("media_type", sa.String(), nullable=False),
        ],
        [_idx("ix_post_media_post_id", "post_media", ["post_id"])],
        copy=(
            ("id", "post_id", "path", "media_type"),
            ("id", "post_id", "path", "media_type"),
        ),
    )

    _recreate(
        "friend_requests",
        _friend_request_columns(with_checks=True),
        _friend_request_indexes(),
        copy=(
            ("id", "sender_id", "recipient_id", "canonical_left",
             "canonical_right", "state", "created_at"),
            ("id", "sender_id", "recipient_id", "canonical_left",
             "canonical_right", "state", "created_at"),
        ),
    )


def downgrade() -> None:
    dialect = op.get_context().dialect.name

    if dialect == "postgresql":
        for table, fk_name in _POST_FKS.items():
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} "
                f"FOREIGN KEY (post_id) REFERENCES posts (id)"
            )
        for table, name in (
            ("activities", "ck_activity_kind"),
            ("follows", "ck_follow_not_self"),
            ("posts", "ck_post_audience"),
            ("friend_requests", "ck_friend_request_state"),
            ("friend_requests", "ck_friend_request_not_self"),
            ("friend_requests", "ck_friend_request_canonical"),
        ):
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.drop_column("activities", "read_at")
        return

    # ------------------------------------------------------------------ SQLite
    _recreate(
        "activities",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("user_id"),
            _user_fk("actor_id"),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        [_idx("ix_activities_user_id", "activities", ["user_id"])],
        copy=(
            ("id", "user_id", "actor_id", "kind", "post_id", "created_at"),
            ("id", "user_id", "actor_id", "kind", "post_id", "created_at"),
        ),
    )

    _recreate(
        "follows",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("follower_id"),
            _user_fk("followed_id"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("follower_id", "followed_id", name="uq_follow_pair"),
        ],
        [
            _idx("ix_follows_follower_id", "follows", ["follower_id"]),
            _idx("ix_follows_followed_id", "follows", ["followed_id"]),
        ],
        copy=(
            ("id", "follower_id", "followed_id", "created_at"),
            ("id", "follower_id", "followed_id", "created_at"),
        ),
    )

    _recreate(
        "posts",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("author_id"),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("audience", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        [_idx("ix_posts_author_id", "posts", ["author_id"])],
        copy=(
            ("id", "author_id", "content", "audience", "created_at", "updated_at"),
            ("id", "author_id", "content", "audience", "created_at", "updated_at"),
        ),
    )

    for table, unique_name in (("saved_posts", "uq_saved_post"), ("reposts", "uq_repost")):
        _recreate(
            table,
            [
                sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                _user_fk("user_id"),
                sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
                sa.Column("created_at", sa.DateTime(), nullable=False),
                sa.UniqueConstraint("user_id", "post_id", name=unique_name),
            ],
            [
                _idx(f"ix_{table}_user_id", table, ["user_id"]),
                _idx(f"ix_{table}_post_id", table, ["post_id"]),
            ],
            copy=(
                ("id", "user_id", "post_id", "created_at"),
                ("id", "user_id", "post_id", "created_at"),
            ),
        )

    _recreate(
        "likes",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _user_fk("user_id"),
            sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "post_id", name="uq_user_post_like"),
        ],
        [
            _idx("ix_likes_user_id", "likes", ["user_id"]),
            _idx("ix_likes_post_id", "likes", ["post_id"]),
        ],
        copy=(
            ("id", "user_id", "post_id", "created_at"),
            ("id", "user_id", "post_id", "created_at"),
        ),
    )

    _recreate(
        "comments",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
            _user_fk("author_id"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        [
            _idx("ix_comments_post_id", "comments", ["post_id"]),
            _idx("ix_comments_author_id", "comments", ["author_id"]),
        ],
        copy=(
            ("id", "post_id", "author_id", "content", "created_at"),
            ("id", "post_id", "author_id", "content", "created_at"),
        ),
    )

    _recreate(
        "post_media",
        [
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("post_id", sa.Integer(), sa.ForeignKey("posts.id"), nullable=False),
            sa.Column("path", sa.String(), nullable=False),
            sa.Column("media_type", sa.String(), nullable=False),
        ],
        [_idx("ix_post_media_post_id", "post_media", ["post_id"])],
        copy=(
            ("id", "post_id", "path", "media_type"),
            ("id", "post_id", "path", "media_type"),
        ),
    )

    _recreate(
        "friend_requests",
        _friend_request_columns(with_checks=False),
        _friend_request_indexes(),
        copy=(
            ("id", "sender_id", "recipient_id", "canonical_left",
             "canonical_right", "state", "created_at"),
            ("id", "sender_id", "recipient_id", "canonical_left",
             "canonical_right", "state", "created_at"),
        ),
    )