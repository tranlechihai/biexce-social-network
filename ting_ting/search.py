"""Privacy-aware post and hashtag search (T-026).

Canonical posts remain in ``posts``. SQLite uses an external-content FTS5
index; PostgreSQL uses ``to_tsvector('simple', content)`` backed by a GIN
expression index. Results deliberately order by stable recency rather than a
dialect-specific rank so pagination semantics match across both engines.
"""

import re
import unicodedata

from sqlalchemy import column, func, literal_column, select, table, text
from sqlalchemy.orm import Session

from ting_ting.keyset import decode_cursor
from ting_ting.models import Post, PostHashtag
from ting_ting import posts

SEARCH_QUERY_MAX = 100
SEARCH_TERM_MAX = 40
SEARCH_TERMS_MAX = 8
_SEARCH_TERM_RE = re.compile(r"\w+", re.UNICODE)


def search_terms(raw: str) -> list[str]:
    """Bound and normalize untrusted input into plain lexical terms.

    Search operators never pass through to FTS5 / tsquery, preventing syntax
    errors and operator injection. Empty or punctuation-only input means no
    results.
    """
    normalized = unicodedata.normalize("NFKC", raw.strip())[:SEARCH_QUERY_MAX]
    result: list[str] = []
    seen: set[str] = set()
    for match in _SEARCH_TERM_RE.finditer(normalized):
        raw_term = match.group(0)
        # Common FTS operators written in operator form are noise, not terms.
        # Lowercase natural-language "or" remains searchable.
        if raw_term in {"AND", "OR", "NOT", "NEAR"}:
            continue
        term = raw_term.casefold()[:SEARCH_TERM_MAX]
        if term and term not in seen:
            seen.add(term)
            result.append(term)
            if len(result) == SEARCH_TERMS_MAX:
                break
    return result


def _visible_search_stmt(viewer_id: int):
    return select(Post).where(
        (
            (Post.author_id == viewer_id)
            | posts._others_visible_condition(viewer_id)
        ),
        *posts._feed_suppression_conditions(viewer_id),
    )


def _apply_cursor(stmt, cursor: str | None):
    if not cursor:
        return stmt
    try:
        created_at, row_id = decode_cursor(cursor)
    except ValueError:
        return stmt
    return stmt.where(
        (Post.created_at < created_at)
        | ((Post.created_at == created_at) & (Post.id < row_id))
    )


def query_post_search(
    db: Session,
    viewer_id: int,
    raw_query: str,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    terms = search_terms(raw_query)
    if not terms:
        return [], None

    stmt = _visible_search_stmt(viewer_id)
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        # Terms are quoted literals joined by explicit AND; no caller syntax
        # reaches MATCH. Double quotes inside terms cannot occur (\w-only).
        fts_query = " AND ".join(f'"{term}"' for term in terms)
        fts_ids = (
            select(literal_column("rowid"))
            .select_from(table("posts_fts", column("rowid")))
            .where(text("posts_fts MATCH :fts_query"))
        )
        stmt = stmt.where(Post.id.in_(fts_ids)).params(fts_query=fts_query)
    elif dialect == "postgresql":
        plain_query = " ".join(terms)
        config = literal_column("'simple'::regconfig")
        vector = func.to_tsvector(config, func.coalesce(Post.content, ""))
        stmt = stmt.where(vector.op("@@")(func.plainto_tsquery(config, plain_query)))
    else:  # validated at startup; deny unknown backends anyway.
        raise ValueError("unsupported database backend")

    stmt = _apply_cursor(stmt, cursor)
    rows = list(db.scalars(
        stmt.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
    ).all())
    return posts._paginate(rows, limit)


def query_hashtag_posts(
    db: Session,
    viewer_id: int,
    tag: str,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    from ting_ting.post_entities import normalize_hashtag

    normalized = normalize_hashtag(tag.lstrip("#"))
    if not normalized or not normalized.replace("_", "").isalnum():
        return [], None
    stmt = _visible_search_stmt(viewer_id).where(
        Post.id.in_(
            select(PostHashtag.post_id).where(PostHashtag.tag == normalized)
        )
    )
    stmt = _apply_cursor(stmt, cursor)
    rows = list(db.scalars(
        stmt.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
    ).all())
    return posts._paginate(rows, limit)
