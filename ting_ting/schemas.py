"""Pydantic request/response schemas for auth, profile, and social graph."""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

USERNAME_MIN = 3
USERNAME_MAX = 30
DISPLAY_NAME_MAX = 60
BIO_MAX = 300
PASSWORD_MIN = 8


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(min_length=USERNAME_MIN, max_length=USERNAME_MAX)
    email: str
    password: str = Field(min_length=PASSWORD_MIN)

    @field_validator("username")
    @classmethod
    def _username_chars(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9_]+$", v.strip().lower()):
            raise ValueError(
                "Username may only contain lowercase letters, digits, and underscores."
            )
        return v.strip().lower()

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("A valid email address is required.")
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN:
            raise ValueError(
                f"Password must be at least {PASSWORD_MIN} characters."
            )
        return v


class ChangePasswordRequest(BaseModel):
    """Rotate the account password (current password required)."""

    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    """Login accepts either username or email as the identifier."""

    identifier: str
    password: str = Field(min_length=1)


class AvatarUploadResponse(BaseModel):
    """Result of an avatar upload (T-025 mobile profile/media API)."""

    avatar_url: str


class UserResponse(BaseModel):
    """Non-sensitive public user representation — never exposes password or token."""

    id: int
    username: str
    email: str
    display_name: str | None = None
    bio: str | None = None
    # T-024: private accounts gate content on follower approval.
    is_private: bool = False

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Profile schemas
# ---------------------------------------------------------------------------

class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(
        default=None,
        min_length=0,
        max_length=DISPLAY_NAME_MAX,
    )
    bio: str | None = Field(default=None, max_length=BIO_MAX)
    # T-024: toggling to public auto-approves pending follow requests.
    is_private: bool | None = None


# ---------------------------------------------------------------------------
# Token response
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # T-021: rotating opaque refresh token (None when the request could not
    # bind a new one, e.g. legacy JWT-only refresh).
    refresh_token: str | None = None


class RefreshRequest(BaseModel):
    """Body for POST /auth/refresh.

    ``refresh_token`` is optional: when absent (or the cookie is used) the
    endpoint falls back to re-minting from a still-valid access JWT — the
    pre-T-021 behavior, kept for backward compatibility.
    """

    refresh_token: str | None = None


class SessionItem(BaseModel):
    """One server-side session as listed by GET /auth/sessions."""

    id: str
    created_at: datetime
    expires_at: datetime
    current: bool = False


# ---------------------------------------------------------------------------
# Social graph schemas
# ---------------------------------------------------------------------------

class FriendRequestRequest(BaseModel):
    """Create a friend request for another user."""

    target_user_id: int = Field(gt=0)


class FriendTransitionRequest(BaseModel):
    """Accept, reject, or process a friend request by its ID."""

    request_id: int = Field(gt=0)


class UserRef(BaseModel):
    """Compact non-sensitive user reference."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None = None


class FriendRequestResponse(BaseModel):
    """Public representation of a friend request."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sender: UserRef
    recipient: UserRef
    state: str  # pending | accepted | rejected
    created_at: str | None = None  # ISO-8601 from API layer


# ---------------------------------------------------------------------------
# Post / feed schemas
# ---------------------------------------------------------------------------

# Maximum allowed post content length.
POST_CONTENT_MAX = 2000

# Maximum allowed comment text length.
COMMENT_TEXT_MAX = 1000

# Bounded pagination limits for feed.
FEED_LIMIT_MIN = 1
FEED_LIMIT_MAX = 100
FEED_LIMIT_DEFAULT = 20
FEED_OFFSET_MIN = 0


class PostCreateRequest(BaseModel):
    """Create a new post."""

    content: str = Field(min_length=1, max_length=POST_CONTENT_MAX)
    audience: str = Field(default="ONLY_ME")  # ONLY_ME | FRIENDS | PUBLIC | FOLLOWERS

    @field_validator("audience")
    @classmethod
    def _audience_allowed(cls, v: str) -> str:
        v = v.strip()
        if v not in ("ONLY_ME", "FRIENDS", "PUBLIC", "FOLLOWERS"):
            raise ValueError("Invalid audience. Allowed: ONLY_ME, FRIENDS, PUBLIC, FOLLOWERS.")
        return v


class PostUpdateRequest(BaseModel):
    """Update editable fields of a post (author only)."""

    content: str | None = Field(default=None, min_length=1, max_length=POST_CONTENT_MAX)
    audience: str | None = Field(default=None)

    @field_validator("audience")
    @classmethod
    def _audience_allowed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v not in ("ONLY_ME", "FRIENDS", "PUBLIC", "FOLLOWERS"):
            raise ValueError("Invalid audience. Allowed: ONLY_ME, FRIENDS, PUBLIC, FOLLOWERS.")
        return v


class PostResponse(BaseModel):
    """Public representation of a post — returned by read endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserRef
    content: str
    audience: str
    created_at: str | None = None
    updated_at: str | None = None

    # Interaction summary (computed, not persisted on Post)
    like_count: int = 0
    comment_count: int = 0
    liked_by_viewer: bool = False
    repost_count: int = 0
    saved_by_viewer: bool = False
    reposted_by_viewer: bool = False
    media: list["PostMediaResponse"] = []


class CommentResponse(BaseModel):
    """Public representation of a comment."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserRef
    content: str
    created_at: str | None = None
    parent_id: int | None = None


class ReportCreateRequest(BaseModel):
    """File a report against a user's post, comment, or bare account.

    ``post_id`` and/or ``comment_id`` pin the content; a comment report
    always anchors to the comment's own post (``post_id`` is overridden by
    the server for consistency).  The target user is always required (the
    account owns the content).
    """

    target_user_id: int = Field(gt=0)
    post_id: int | None = Field(default=None, gt=0)
    comment_id: int | None = Field(default=None, gt=0)
    reason: Literal["spam", "harassment", "hate_speech", "false_info", "other"]


class ReportResponse(BaseModel):
    """Public-safe report row — moderator surface only."""

    id: int
    reporter: UserRef | None = None
    target_user: UserRef | None = None
    post_id: int | None = None
    comment_id: int | None = None
    reason: str
    status: str
    resolution_note: str | None = None
    resolved_by: dict | None = None
    created_at: str | None = None
    resolved_at: str | None = None


class CommentCreateRequest(BaseModel):
    """Create a comment on a visible post.

    ``parent_comment_id`` turns the comment into a one-level reply to a
    top-level comment of the same post.
    """

    content: str = Field(min_length=1, max_length=COMMENT_TEXT_MAX)
    parent_comment_id: int | None = Field(default=None, gt=0)


class CommentUpdateRequest(BaseModel):
    """Edit an existing comment (author only)."""

    content: str = Field(min_length=1, max_length=COMMENT_TEXT_MAX)


class UserSearchItem(BaseModel):
    """One search result — a user known to the current viewer."""

    id: int
    username: str
    display_name: str | None = None
    relationship: str
    followed: bool = False


class UserListResponse(BaseModel):
    """Paged user-search response; the next cursor travels in the
    ``X-Next-Cursor`` response header instead (see feed)."""

    items: list[UserSearchItem]


class UserPublicResponse(BaseModel):
    """Public profile of another user as seen by the viewer.

    When the pair is blocked (either direction) the identifying profile fields
    are redacted and the counts are ``None`` — no profile data leaks past a
    block.
    """

    id: int
    username: str
    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    relationship: str
    # T-024: lets clients know a follow will be pending until approved.
    is_private: bool = False
    follower_count: int | None = None
    following_count: int | None = None
    friend_count: int | None = None


class RelationshipState(BaseModel):
    """Current relationship between two users as observed by the viewer.

    Codes:
    * ``none`` — no relationship at all
    * ``pending_outgoing`` — viewer sent a request to the target
    * ``pending_incoming`` — target sent a request to viewer (awaiting response)
    * ``friends`` — accepted friendship
    * ``blocked_by_me`` — viewer has blocked the target
    * ``blocked_by_them`` — target has blocked the viewer
    """

    state: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Extended API schemas
# ---------------------------------------------------------------------------

class ExtendedProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX)
    bio: str | None = Field(default=None, max_length=BIO_MAX)
    is_private: bool | None = None
    birthday: str | None = None
    gender: Literal["female", "male", "non_binary", "prefer_not_to_say"] | None = None
    location: str | None = Field(default=None, max_length=100)
    occupation: str | None = Field(default=None, max_length=100)
    website: str | None = Field(default=None, max_length=300)
    avatar_url: str | None = Field(default=None, max_length=500)

    @field_validator("birthday")
    @classmethod
    def _birthday_format(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Birthday must use YYYY-MM-DD format.")
        return value

    @field_validator("website", "avatar_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must use http or https.")
        return value


class ExtendedProfileResponse(BaseModel):
    id: int
    username: str
    email: str
    display_name: str | None = None
    bio: str | None = None
    is_private: bool = False
    birthday: str | None = None
    gender: str | None = None
    location: str | None = None
    occupation: str | None = None
    website: str | None = None
    avatar_url: str | None = None


class ActivityResponse(BaseModel):
    id: int
    actor: UserRef
    kind: str
    post_id: int | None = None
    created_at: str | None = None


class NotificationResponse(BaseModel):
    """Public notification — an ``Activity`` row with read state."""

    id: int
    actor: UserRef
    kind: str
    post_id: int | None = None
    created_at: str | None = None
    is_read: bool = False


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    next_cursor: str | None = None


class UnreadCountResponse(BaseModel):
    unread: int


class ToggleResponse(BaseModel):
    active: bool
    # T-024: follow endpoints report the edge state — "pending" while a
    # private account must approve the request.  None for non-follow uses.
    state: str | None = None


class FollowRequestResponse(BaseModel):
    """Public representation of a follow request (a pending follow edge).

    ``requester`` follows ``owner`` and awaits the owner's decision.
    """

    id: int
    requester: UserRef
    owner: UserRef
    status: str  # always "pending" while listable
    created_at: str | None = None


class PostMediaResponse(BaseModel):
    id: int
    post_id: int
    url: str
    media_type: Literal["image", "video"]


# PostResponse references PostMediaResponse defined above it via a string
# annotation — resolve it now that both classes exist.
PostResponse.model_rebuild()
