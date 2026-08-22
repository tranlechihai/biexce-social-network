"""SQLAlchemy ORM model definitions."""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """Core user record — identity, credentials, and basic profile."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        unique=True, nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(
        unique=True, nullable=False, index=True,
    )
    password_hash: Mapped[str] = mapped_column(nullable=False)

    # Editable profile fields
    display_name: Mapped[str | None] = mapped_column(default=None)
    bio: Mapped[str | None] = mapped_column(default=None)

    # Moderation
    is_moderator: Mapped[bool] = mapped_column(nullable=False, default=False)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # T-024: private account.  When set, the user's PUBLIC posts are visible
    # only to friends and ACTIVE followers, and following becomes a
    # pending request that the user must approve (see ``Follow.status``).
    is_private: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"),
    )

    # Account lifecycle (T-023): self-deactivation. Reversible — the user can
    # sign back in (login is NOT blocked, unlike a ban) and reactivate. While
    # set, the user is hidden from feeds, search, public profiles and graphs.
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None,
    )


class DeletedAccount(Base):
    """Tombstone left when a user deletes their account (T-023).

    Locks the deleted account's ``username`` and ``email`` for a retention
    window (30 days) so the identifiers are not immediately reusable — that
    prevents a new registrant from impersonating someone who just left and
    from hijacking in-flight deep links / notifications that still name them.
    After the window the row may be purged (T-030 jobs) and the identifiers
    become reusable. The actual account data is physically deleted on deletion
    (not kept in this row) — this table only reserves the names.
    """

    __tablename__ = "deleted_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(unique=True, nullable=False, index=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )


class UserProfile(Base):
    """Extended optional profile data kept separate for additive MVP evolution."""

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), primary_key=True,
    )
    birthday: Mapped[str | None] = mapped_column(default=None)
    gender: Mapped[str | None] = mapped_column(default=None)
    location: Mapped[str | None] = mapped_column(default=None)
    occupation: Mapped[str | None] = mapped_column(default=None)
    website: Mapped[str | None] = mapped_column(default=None)
    avatar_url: Mapped[str | None] = mapped_column(default=None)
    avatar_path: Mapped[str | None] = mapped_column(default=None)


class Follow(Base):
    """Directed follow edge: ``follower_id`` follows ``followed_id``.

    T-024: ``status`` — ``pending`` is a follow REQUEST to a private account
    (the target must approve), ``active`` is a live follow.  Approving a
    request flips pending → active; rejecting, unfollowing or cancelling all
    DELETE the row — a follow edge that does not apply simply does not
    exist, which keeps the unique pair and every count trivially correct.
    All readers (feeds, lists, counts) must filter on ``status == 'active'``
    except the follow-request management endpoints.
    """

    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("follower_id", "followed_id", name="uq_follow_pair"),
        CheckConstraint("follower_id <> followed_id", name="ck_follow_not_self"),
        CheckConstraint("status IN ('pending', 'active')", name="ck_follow_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    follower_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    followed_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Activity(Base):
    """Notification row: ``user_id`` was notified about ``actor_id``'s action.

    ``read_at`` is NULL while the notification is unread.  Deleting the
    referenced post cascades to its notification rows (no leaked pointers).
    ``kind`` ``'follow_request'`` (T-024) marks a pending follow on a
    private account — it becomes a plain ``'follow'`` notification for the
    requester once the target approves or the account goes public.
    """

    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('follow','like','comment','repost','follow_request','mention')",
            name="ck_activity_kind",
        ),
        Index(
            "ux_activities_unread_dedup",
            "user_id", "actor_id", "kind", "source_key",
            unique=True,
            sqlite_where=text("read_at IS NULL AND source_key IS NOT NULL"),
            postgresql_where=text("read_at IS NULL AND source_key IS NOT NULL"),
        ),
        Index("ix_activities_user_created_id", "user_id", "created_at", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(nullable=False)
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True,
    )
    # Deterministic event identity used by the unread partial unique index.
    # Nullable preserves historical rows and backwards-compatible callers.
    source_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class NotificationPreference(Base):
    """Per-user notification creation preferences (T-027).

    A missing row means every kind is enabled. Preferences gate only future
    rows; existing notification history is never deleted or hidden.
    """

    __tablename__ = "notification_preferences"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    follow_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    follow_request_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    like_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    comment_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    repost_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    mention_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )


class SavedPost(Base):
    __tablename__ = "saved_posts"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_saved_post"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Repost(Base):
    __tablename__ = "reposts"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_repost"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PostMedia(Base):
    __tablename__ = "post_media"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    path: Mapped[str] = mapped_column(nullable=False)
    media_type: Mapped[str] = mapped_column(nullable=False, default="image")


class FriendRequest(Base):
    """Directed friend-request record.

    * A ``pending`` row means user ``sender_id`` has asked ``recipient_id`` to
      be friends.
    * When the *recipient* accepts, the row transitions to ``accepted`` and the
      relationship becomes bidirectional friendship (no extra row).
    * When rejected, the row transitions to ``rejected``.
    * ``unfriend`` sets both sides to ``rejected`` in a single transaction.
    * Only one row with state ``pending`` or ``accepted`` may exist per
      unordered pair (enforced by a unique partial index; see models.py).
    """

    __tablename__ = "friend_requests"
    __table_args__ = (
        # Only one active (pending/accepted) row per canonical unordered pair.
        # Historical "rejected" rows are NOT constrained — allows repeated
        # reject → new-request → reject flows without UNIQUE violations.
        Index(
            "ix_active_pair",
            "canonical_left",
            "canonical_right",
            unique=True,
            sqlite_where=text("state IN ('pending', 'accepted')"),
            postgresql_where=text("state IN ('pending', 'accepted')"),
        ),
        CheckConstraint(
            "state IN ('pending', 'accepted', 'rejected')",
            name="ck_friend_request_state",
        ),
        CheckConstraint("sender_id <> recipient_id", name="ck_friend_request_not_self"),
        CheckConstraint("canonical_left < canonical_right", name="ck_friend_request_canonical"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    sender_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )

    # Canonical left/right = the smaller/larger user-id — used for the
    # unique-pair check.
    canonical_left: Mapped[int] = mapped_column(nullable=False, index=True)
    canonical_right: Mapped[int] = mapped_column(nullable=False)

    state: Mapped[str] = mapped_column(
        nullable=False,
        default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )


class Block(Base):
    """Directed block record — bilateral effect.

    When user A blocks user B:
    * A ``Block(A, B)`` row is created.
    * Any existing friendship / pending request between A and B is removed.
    * Either party cannot create a friend request while the block exists.
    * Unblock simply removes the row and does NOT restore any relationship.
    """

    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_block_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    blocker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    blocked_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )


class Post(Base):
    """Text post with audience control.

    Audiences: ``ONLY_ME`` (author only), ``FRIENDS`` (author + current friends),
    ``PUBLIC`` (anyone, subject to the author's privacy setting) and
    ``FOLLOWERS`` (author + active followers — T-024).
    Visibility is re-evaluated at every read (including feed) against the
    current block/friend/follow graph and the author's privacy flag — a
    stored post ID alone grants no access.
    """

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "audience IN ('ONLY_ME', 'FRIENDS', 'PUBLIC', 'FOLLOWERS')",
            name="ck_post_audience",
        ),
        # Feed ordering is (created_at DESC, id DESC) — one index serves the
        # keyset scan once the visibility filter narrows candidates.
        Index("ix_posts_created_at_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    content: Mapped[str] = mapped_column(nullable=False)
    audience: Mapped[str] = mapped_column(
        nullable=False, default="ONLY_ME",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )

    # Deleting a post deletes every dependent row at the database level
    # (ON DELETE CASCADE).  ``passive_deletes=True`` keeps the ORM from
    # loading child collections just to re-issue the deletes.
    media: Mapped[list["PostMedia"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    likes: Mapped[list["Like"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    comments: Mapped[list["Comment"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    saved_posts: Mapped[list["SavedPost"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    reposts: Mapped[list["Repost"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    activities: Mapped[list["Activity"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    mentions: Mapped[list["PostMention"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )
    hashtags: Mapped[list["PostHashtag"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
    )


class PostMention(Base):
    """A resolved ``@username`` entity in a post (T-026).

    Text that does not resolve to a current active account stays plain text;
    duplicate mentions of the same account collapse to one row per post.
    """

    __tablename__ = "post_mentions"
    __table_args__ = (
        UniqueConstraint("post_id", "mentioned_user_id", name="uq_post_mention"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    mentioned_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )


class PostHashtag(Base):
    """One normalized hashtag attached to a post (T-026)."""

    __tablename__ = "post_hashtags"
    __table_args__ = (
        UniqueConstraint("post_id", "tag", name="uq_post_hashtag"),
        Index("ix_post_hashtags_tag_post", "tag", "post_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )


# ``Base.metadata.create_all`` is the fast path for isolated SQLite tests.
# Alembic owns real databases, but create_all must still produce the same
# migration-only FTS artifacts so tests cannot be stamped at head without a
# working search index.  The FTS table is intentionally NOT mapped: backup /
# copy tooling transfers canonical posts and rebuilds search indexes instead
# of copying derived shadow rows.
_SQLITE_POSTS_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5("
    "content, content='posts', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 0')"
)
_SQLITE_POSTS_FTS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN "
    "INSERT INTO posts_fts(rowid, content) VALUES (new.id, new.content); END",
    "CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN "
    "INSERT INTO posts_fts(posts_fts, rowid, content) "
    "VALUES ('delete', old.id, old.content); END",
    "CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE OF content ON posts BEGIN "
    "INSERT INTO posts_fts(posts_fts, rowid, content) "
    "VALUES ('delete', old.id, old.content); "
    "INSERT INTO posts_fts(rowid, content) VALUES (new.id, new.content); END",
)
event.listen(
    Post.__table__, "after_create",
    DDL(_SQLITE_POSTS_FTS_DDL).execute_if(dialect="sqlite"),
)
for _fts_trigger in _SQLITE_POSTS_FTS_TRIGGERS:
    event.listen(
        Post.__table__, "after_create",
        DDL(_fts_trigger).execute_if(dialect="sqlite"),
    )
event.listen(
    Post.__table__, "before_drop",
    DDL("DROP TABLE IF EXISTS posts_fts").execute_if(dialect="sqlite"),
)


class Like(Base):
    """User like on a post — at most one per (user, post) pair.

    Uniqueness enforced at the database level so repeated POST requests always
    converge to exactly one row (idempotent like/unlike).
    """

    __tablename__ = "likes"

    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_user_post_like"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )


class Mute(Base):
    """Directed, unilateral mute — the viewer silences one user OR one post.

    Exactly one of ``target_id`` (user) / ``post_id`` is set:

    * user mute (``target_id``): the target's posts leave the muter's feeds
      and the target's notifications are hidden.  Does NOT remove any
      relationship (follow/friend/request) and has no effect on the target.
    * post mute (``post_id``): that single post is hidden from the muter's
      feeds; direct reads are unaffected.

    Deleting a referenced post cascades to its mute rows.
    """

    __tablename__ = "mutes"
    __table_args__ = (
        CheckConstraint("muted_by <> target_id", name="ck_mute_not_self"),
        CheckConstraint(
            "(target_id IS NOT NULL) <> (post_id IS NOT NULL)",
            name="ck_mute_exactly_one_target",
        ),
        # Partial unique indexes — NULLs are distinct in a plain UNIQUE, so
        # each mute flavor gets its own partial constraint.
        Index(
            "uq_mute_user", "muted_by", "target_id", unique=True,
            sqlite_where=text("target_id IS NOT NULL"),
            postgresql_where=text("target_id IS NOT NULL"),
        ),
        Index(
            "uq_mute_post", "muted_by", "post_id", unique=True,
            sqlite_where=text("post_id IS NOT NULL"),
            postgresql_where=text("post_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    muted_by: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    # Muted user (user mutes). NULL for post mutes.
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    # Muted post (post mutes). NULL for user mutes.
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )


class AuthSession(Base):
    """Server-side session registry — enables real logout/revocation.

    The JWT (short-lived) carries ``sid``; every request validates the
    session row.  Revocation paths: logout (this session), logout-all /
    password change (all other sessions), user deletion (FK cascade).
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None,
    )


class RefreshToken(Base):
    """Rotating opaque refresh token bound to a server-side session (T-021).

    Only the SHA-256 hash of the token is stored. Every successful use
    rotates the token: the row is revoked and ``replaced_by_id`` points at
    the successor. Re-presenting an already-rotated token is a replay —
    the whole session is killed as a side effect.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None,
    )
    replaced_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
    )


class Report(Base):
    """Abuse report — evidence separated from public content.

    One row per (reporter, target): re-reporting the same target returns the
    existing row (idempotent, no duplicate spam).  ``target_user_id`` is the
    reported user; ``post_id``/``comment_id`` pin the exact content.

    Only moderators may list/resolve reports and take enforcement actions.
    Resolution records the acting moderator and their note, and a banned
    target is resolved automatically when a moderator bans them.
    """

    # A report pins at most two content rows: a post report sets post_id only,
    # a comment report sets comment_id (and its anchoring post_id), and a bare
    # account report sets neither.  The functional unique index below keeps one
    # row per (reporter, target, content).
    #
    # COALESCE applies to comment_id only (not post_id): a post report's
    # comment_id is NULL, and a plain unique constraint treats NULLs as
    # distinct, so racing post-report inserts would otherwise duplicate.  But
    # post_id must stay raw — when a moderator deletes a post, the FK SET NULL
    # turns an anchored report into (reporter, target, NULL, 0), and a fully
    # COALESCEd index would make that row collide with an existing bare
    # account report (NULL != 0, so a raw post_id can never collide).
    __tablename__ = "reports"
    __table_args__ = (
        Index(
            "ux_reports_dedup",
            "reporter_id",
            "target_user_id",
            "post_id",
            text("coalesce(comment_id, 0)"),
            unique=True,
        ),
        CheckConstraint(
            "reason IN ('spam','harassment','hate_speech','false_info','other')",
            name="ck_report_reason",
        ),
        CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_report_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # A report is moderation evidence: it outlives BOTH the content it pinned
    # (post/comment -> SET NULL) and the accounts it references (user -> SET
    # NULL). Deleting a user therefore anonymizes their reports (NULL refs)
    # instead of destroying the audit trail (T-023).
    reporter_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    target_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # SET NULL (not CASCADE): a report outlives the content it pinned — the
    # audit trail must survive moderation deletion of the post/comment.
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    reason: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None,
    )


class Comment(Base):
    """Text comment on a post.

    Created by an authorized user (someone who currently sees the post).
    Author or post-owner can delete.  The author can edit their own comment.
    One level of nesting: a comment may reply to a top-level comment of the
    same post (``parent_comment_id``); replies to replies are rejected.
    Deleting a top-level comment cascades to its replies.
    """

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    parent_comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=None,
    )

    replies: Mapped[list["Comment"]] = relationship(
        foreign_keys=[parent_comment_id],
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
