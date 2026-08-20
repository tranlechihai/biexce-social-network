"""Opaque keyset (``created_at, id``) pagination cursors.

Cursors are base64url tokens encoding ``"<iso8601>|<row id>"``.  Callers treat
them as opaque strings; decoding failures raise ``ValueError`` and callers
decide how to degrade (never a 500).
"""

import base64
from datetime import datetime


def encode_cursor(row) -> str:
    raw = f"{row.created_at.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts, row_id = raw.split("|", 1)
        return datetime.fromisoformat(ts), int(row_id)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


def encode_pair(key: str, row_id: int) -> str:
    """Opaque ascending keyset cursor for string-keyed scans (e.g. usernames)."""
    return base64.urlsafe_b64encode(f"{key}|{row_id}".encode()).decode("ascii")


def decode_pair(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        key, row_id = raw.rsplit("|", 1)
        return key, int(row_id)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc
