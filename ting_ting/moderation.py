"""Moderation service — abuse reports, bans, and moderator enforcement.

This module is the single writer for ``Report`` rows and ban transitions.

Rules:
* Reports: one row per (reporter, target, content) — re-reporting returns the
  existing row.  Self-reports are rejected.  Reporters cannot read reports.
* Bans: only moderators.  Banning freezes the account (auth rejected at the
  next check), severs that user's follows and friend requests, and resolves
  all pending reports against them (audit: ``resolved_by`` + note).  Unban is
  the inverse and idempotent; it does NOT restore severed relationships.
* Moderator content removal (delete post/comment) is the enforcement path
  for reported content; the acting moderator is logged on the report row
  when it is resolved.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting.models import (
    Comment, Follow, FriendRequest, Post, Report, User,
)

REPORT_REASONS = ("spam", "harassment", "hate_speech", "false_info", "other")
PENDING = "pending"
RESOLVED = "resolved"
DISMISSED = "dismissed"

#: Moderation evidence/audit (report rows) is kept for this long, then purged
#: by the T-030 jobs worker. Until the purge lands, expired rows are hidden
#: at the read boundaries (``list_reports`` / resolve guards) — the retention
#: decision of 2026-08-21.
EVIDENCE_RETENTION_DAYS = 30


def retention_cutoff(now: datetime | None = None) -> datetime:
    """Naive-UTC boundary: reports created before it are expired."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.replace(tzinfo=None) - timedelta(days=EVIDENCE_RETENTION_DAYS)


def report_expired(report: Report, now: datetime | None = None) -> bool:
    """True once a report is past the evidence retention window."""
    created = report.created_at
    if created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    return created < retention_cutoff(now)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def create_report(
    db: Session,
    reporter: User,
    target_user_id: int,
    reason: str,
    post_id: int | None = None,
    comment_id: int | None = None,
) -> Report:
    """File (or return the existing) report.  Idempotent per exact target.

    Raises ``ValueError``:
    * ``invalid_reason`` — reason not in ``REPORT_REASONS``;
    * ``self_report`` — reporting your own account;
    * ``content_requires_post`` — ``comment_id`` without ``post_id``.
    """
    if reason not in REPORT_REASONS:
        raise ValueError("invalid_reason")
    if reporter.id == target_user_id:
        raise ValueError("self_report")
    if comment_id is not None and post_id is None:
        raise ValueError("content_requires_post")

    predicate = (
        Report.reporter_id == reporter.id,
        Report.target_user_id == target_user_id,
        Report.post_id == post_id,
        Report.comment_id == comment_id,
    )
    existing = db.execute(select(Report).where(*predicate)).scalar_one_or_none()
    if existing is not None:
        return existing

    # A racing request can insert the same report between the check above and
    # our flush; the NULL-safe index (ux_reports_dedup, migration 0012)
    # rejects the duplicate as an IntegrityError.  The savepoint rolls back
    # only our rows, then we converge to the winner's row — the same pattern
    # as the follow toggle.  (Racing *bare account* reports keep NULL
    # post_ids, which SQL treats as distinct; those duplicates are accepted
    # by design — see the 0012 docstring.)
    report = None
    try:
        with db.begin_nested():
            report = Report(
                reporter_id=reporter.id,
                target_user_id=target_user_id,
                post_id=post_id,
                comment_id=comment_id,
                reason=reason,
            )
            db.add(report)
            db.flush()
    except IntegrityError:
        from sqlalchemy.orm.attributes import instance_state
        if report is not None and instance_state(report).session_id is not None:
            db.expunge(report)
        existing = db.execute(
            select(Report).where(*predicate)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise
    return report


def list_reports(
    db: Session,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> list[Report]:
    """Mod-only report queue, newest first, minus rows past the evidence
    retention window (read-side filter until the T-030 purge lands)."""
    if status_filter is not None and status_filter not in (
        PENDING, RESOLVED, DISMISSED,
    ):
        raise ValueError("invalid_status")
    conditions = [Report.created_at >= retention_cutoff(now)]
    if status_filter:
        conditions.append(Report.status == status_filter)
    return list(
        db.scalars(
            select(Report)
            .where(*conditions)
            .order_by(Report.created_at.desc(), Report.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )


def resolve_report(
    db: Session,
    moderator: User,
    report: Report,
    dismiss: bool = False,
    note: str | None = None,
) -> Report:
    """Resolve or dismiss a pending report, recording the acting moderator.

    Raises ``ValueError``: ``not_pending``.
    """
    if report.status != PENDING:
        raise ValueError("not_pending")
    report.status = DISMISSED if dismiss else RESOLVED
    report.resolved_by = moderator.id
    report.resolved_at = datetime.now(timezone.utc)
    report.resolution_note = note
    db.flush()
    return report


# ---------------------------------------------------------------------------
# Ban / unban
# ---------------------------------------------------------------------------

def _sever_relationships(db: Session, user_id: int) -> None:
    """Remove follows (both directions) and friend-request rows for the user."""
    for follow in db.scalars(
        select(Follow).where(
            or_(
                Follow.follower_id == user_id,
                Follow.followed_id == user_id,
            )
        )
    ).all():
        db.delete(follow)

    for req in db.scalars(
        select(FriendRequest).where(
            or_(
                FriendRequest.sender_id == user_id,
                FriendRequest.recipient_id == user_id,
            )
        )
    ).all():
        db.delete(req)


def ban_user(db: Session, moderator: User, target: User) -> User:
    """Ban ``target`` (moderators only) — idempotent.

    Effects: ``banned_at`` set; follows + friend requests severed; every
    pending report against the target resolved with the acting moderator.
    Raises ``ValueError``: ``self_ban``.
    """
    if moderator.id == target.id:
        raise ValueError("self_ban")

    now = datetime.now(timezone.utc)
    if target.banned_at is None:
        target.banned_at = now
        _sever_relationships(db, target.id)

    db.execute(
        Report.__table__.update()
        .where(
            Report.target_user_id == target.id,
            Report.status == PENDING,
        )
        .values(
            status=RESOLVED,
            resolved_by=moderator.id,
            resolved_at=now,
            resolution_note="User banned.",
        )
    )
    db.flush()
    return target


def unban_user(db: Session, moderator: User, target: User) -> User:
    """Lift a ban (moderators only) — idempotent. Does not restore severed
    follows or friend requests."""
    if moderator.id == target.id:
        raise ValueError("self_unban")
    if target.banned_at is not None:
        target.banned_at = None
        db.flush()
    return target


# ---------------------------------------------------------------------------
# Moderator content removal
# ---------------------------------------------------------------------------

def delete_post_moderation(db: Session, post: Post) -> Post:
    """Delete a post as a moderation action — no author check (the caller
    enforces moderator authorization)."""
    db.delete(post)
    db.flush()
    return post


def delete_comment_moderation(db: Session, comment: Comment) -> Comment:
    """Delete a comment as a moderation action — replies cascade (DB)."""
    db.delete(comment)
    db.flush()
    return comment
