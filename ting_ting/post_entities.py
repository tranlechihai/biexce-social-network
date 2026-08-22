"""Extraction and reconciliation of post mentions / hashtags (T-026).

The post text remains canonical.  These relational rows are derived indexes
used for mobile linkification, exact hashtag lookup and mention delivery.
Reconciliation is shared by API and web because both write through
``ting_ting.posts``.
"""

from collections import defaultdict
from datetime import datetime, timezone
import re
import unicodedata
from urllib.parse import quote

from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ting_ting.models import Post, PostHashtag, PostMention, User
from ting_ting.user_state import not_actively_banned_clause

MAX_ENTITIES_PER_POST = 20
HASHTAG_MAX = 64

# Usernames are already normalized to lowercase ASCII by registration.
# The left boundary avoids treating the domain part of an email as a mention.
_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{3,30})(?![A-Za-z0-9_])"
)
# Unicode letters/digits are accepted; underscore may appear after the first
# character.  NFKC + casefold gives one canonical form for exact tag lookup.
_HASHTAG_RE = re.compile(r"(?<!\w)#([^\W_][\w]{0,63})", re.UNICODE)
_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9_@])(?P<mention>@[A-Za-z0-9_]{3,30})(?![A-Za-z0-9_])"
    r"|(?<!\w)(?P<hashtag>#[^\W_][\w]{0,63})",
    re.UNICODE,
)


def _unique_limited(values, limit: int = MAX_ENTITIES_PER_POST) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
            if len(result) == limit:
                break
    return result


def extract_mentions(content: str) -> list[str]:
    """Return unique normalized usernames in first-appearance order."""
    return _unique_limited(match.group(1).lower() for match in _MENTION_RE.finditer(content))


def normalize_hashtag(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw).casefold()[:HASHTAG_MAX]


def extract_hashtags(content: str) -> list[str]:
    """Return unique normalized tags (without ``#``), first appearance first."""
    return _unique_limited(
        normalize_hashtag(match.group(1)) for match in _HASHTAG_RE.finditer(content)
    )


def linkify_post_content(content: str) -> Markup:
    """Escape all user text, then add only controlled entity links.

    Returning Markup is safe because every original slice and label passes
    through ``escape``; href values are generated from parser-constrained
    tokens, never raw HTML.
    """
    parts: list[Markup] = []
    position = 0
    for match in _LINK_RE.finditer(content):
        parts.append(escape(content[position:match.start()]))
        label = match.group(0)
        if match.group("mention"):
            username = label[1:].lower()
            href = f"/web/profile/{quote(username)}"
            css_class = "post-mention"
        else:
            tag = normalize_hashtag(label[1:])
            href = f"/web/search?tag={quote(tag)}"
            css_class = "post-hashtag"
        parts.append(Markup(
            f'<a class="{css_class}" href="{href}">{escape(label)}</a>'
        ))
        position = match.end()
    parts.append(escape(content[position:]))
    return Markup("").join(parts)


def reconcile_post_entities(db: Session, post: Post) -> list[int]:
    """Synchronize derived rows with ``post.content``.

    Returns newly-mentioned active user ids.  The caller decides whether each
    target may currently see the post before recording a notification.
    Existing posts are reconciled without duplicate rows; removed entities are
    deleted, while historical notifications intentionally remain historical.
    """
    usernames = extract_mentions(post.content)
    users = []
    if usernames:
        users = db.scalars(
            select(User).where(
                User.username.in_(usernames),
                not_actively_banned_clause(),
                User.deactivated_at.is_(None),
            )
        ).all()
    by_username = {user.username: user for user in users}
    desired_user_ids = {
        by_username[name].id for name in usernames if name in by_username
    }

    existing_mentions = db.scalars(
        select(PostMention).where(PostMention.post_id == post.id)
    ).all()
    existing_user_ids = {row.mentioned_user_id for row in existing_mentions}
    for row in existing_mentions:
        if row.mentioned_user_id not in desired_user_ids:
            db.delete(row)
    now = datetime.now(timezone.utc)
    new_user_ids = sorted(desired_user_ids - existing_user_ids)
    for user_id in new_user_ids:
        db.add(PostMention(
            post_id=post.id,
            mentioned_user_id=user_id,
            created_at=now,
        ))

    desired_tags = set(extract_hashtags(post.content))
    existing_tags = db.scalars(
        select(PostHashtag).where(PostHashtag.post_id == post.id)
    ).all()
    existing_tag_names = {row.tag for row in existing_tags}
    for row in existing_tags:
        if row.tag not in desired_tags:
            db.delete(row)
    for tag in sorted(desired_tags - existing_tag_names):
        db.add(PostHashtag(post_id=post.id, tag=tag, created_at=now))

    db.flush()
    return new_user_ids


def post_entity_maps(
    db: Session,
    post_ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Batch entity metadata for PostResponse assembly (no N+1)."""
    mentions: dict[int, list[str]] = defaultdict(list)
    hashtags: dict[int, list[str]] = defaultdict(list)
    if not post_ids:
        return mentions, hashtags

    mention_rows = db.execute(
        select(PostMention.post_id, User.username)
        .join(User, User.id == PostMention.mentioned_user_id)
        .where(
            PostMention.post_id.in_(post_ids),
            not_actively_banned_clause(),
            User.deactivated_at.is_(None),
        )
        .order_by(PostMention.id)
    ).all()
    for post_id, username in mention_rows:
        mentions[post_id].append(username)

    hashtag_rows = db.execute(
        select(PostHashtag.post_id, PostHashtag.tag)
        .where(PostHashtag.post_id.in_(post_ids))
        .order_by(PostHashtag.id)
    ).all()
    for post_id, tag in hashtag_rows:
        hashtags[post_id].append(tag)
    return mentions, hashtags
