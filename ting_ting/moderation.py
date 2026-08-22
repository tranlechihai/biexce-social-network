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

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ting_ting.models import (
    Comment, Follow, FriendRequest, ModerationAction, Post, Report, User,
    UserWarning,
)
from ting_ting.user_state import ROLE_RANK, is_actively_banned, is_staff

REPORT_REASONS = ("spam", "harassment", "hate_speech", "false_info", "other")
PENDING = "pending"
RESOLVED = "resolved"
DISMISSED = "dismissed"

#: Moderation evidence/audit (report rows) is kept for this long, then purged
#: by the T-030 jobs worker. Until the purge lands, expired rows are hidden
#: at the read boundaries (``list_reports`` / resolve guards) — the retention
#: decision of 2026-08-21.
EVIDENCE_RETENTION_DAYS = 30
ROLES = ("user", "moderator", "admin")


def _clean_reason(reason: str) -> str:
    value = reason.strip()
    if not value or len(value) > 120:
        raise ValueError("invalid_reason")
    return value


def _require_staff(actor: User) -> None:
    if not is_staff(actor):
        raise ValueError("staff_required")


def _require_can_enforce(actor: User, target: User) -> None:
    _require_staff(actor)
    if actor.id == target.id:
        raise ValueError("self_action")
    if ROLE_RANK[actor.role] <= ROLE_RANK[target.role]:
        raise ValueError("insufficient_role")


def _record_action(
    db: Session,
    actor: User,
    action_type: str,
    *,
    target: User | None = None,
    reason: str | None = None,
    note: str | None = None,
    resource_type: str | None = None,
    resource_id: int | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
) -> ModerationAction:
    action = ModerationAction(
        actor_id=actor.id,
        target_user_id=target.id if target else None,
        action_type=action_type,
        reason=reason,
        note=note,
        resource_type=resource_type,
        resource_id=resource_id,
        previous_state=previous_state,
        new_state=new_state,
    )
    db.add(action)
    return action


def is_user_banned(user: User, now: datetime | None = None) -> bool:
    return is_actively_banned(user, now)


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
    _require_staff(moderator)
    target = db.get(User, report.target_user_id) if report.target_user_id else None
    if target is not None:
        _require_can_enforce(moderator, target)
    if report.status != PENDING:
        raise ValueError("not_pending")
    report.status = DISMISSED if dismiss else RESOLVED
    report.resolved_by = moderator.id
    report.resolved_at = datetime.now(timezone.utc)
    report.resolution_note = note
    _record_action(
        db,
        moderator,
        "report_dismissed" if dismiss else "report_resolved",
        target=target,
        reason=report.reason,
        note=note,
        resource_type="report",
        resource_id=report.id,
        previous_state=PENDING,
        new_state=report.status,
    )
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


def ban_user(
    db: Session,
    moderator: User,
    target: User,
    *,
    reason: str = "policy_violation",
    expires_at: datetime | None = None,
    note: str | None = None,
) -> User:
    """Ban ``target`` (moderators only) — idempotent.

    Effects: ``banned_at`` set; follows + friend requests severed; every
    pending report against the target resolved with the acting moderator.
    Raises ``ValueError``: ``self_ban``.
    """
    try:
        _require_can_enforce(moderator, target)
    except ValueError as exc:
        if exc.args[0] == "self_action":
            raise ValueError("self_ban") from None
        raise

    reason = _clean_reason(reason)
    if expires_at is not None:
        comparable = expires_at
        if comparable.tzinfo is not None:
            comparable = comparable.astimezone(timezone.utc).replace(tzinfo=None)
        if comparable <= datetime.now(timezone.utc).replace(tzinfo=None):
            raise ValueError("invalid_expiry")

    now = datetime.now(timezone.utc)
    if not is_actively_banned(target, now):
        target.banned_at = now
        target.banned_until = expires_at
        target.ban_reason = reason
        target.banned_by = moderator.id
        _sever_relationships(db, target.id)
        _record_action(
            db,
            moderator,
            "user_banned",
            target=target,
            reason=reason,
            note=note,
            resource_type="user",
            previous_state="active",
            new_state="banned",
        )

    updated_report_ids = list(db.scalars(
        update(Report)
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
        .returning(Report.id)
    ).all())
    if updated_report_ids:
        db.execute(insert(ModerationAction), [
            {
                "actor_id": moderator.id,
                "target_user_id": target.id,
                "action_type": "report_resolved",
                "reason": "ban_enforcement",
                "note": "Resolved by user ban.",
                "resource_type": "report",
                "resource_id": report_id,
                "previous_state": PENDING,
                "new_state": RESOLVED,
                "created_at": now,
            }
            for report_id in updated_report_ids
        ])
    db.flush()
    return target


def unban_user(db: Session, moderator: User, target: User) -> User:
    """Lift a ban (moderators only) — idempotent. Does not restore severed
    follows or friend requests."""
    try:
        _require_can_enforce(moderator, target)
    except ValueError as exc:
        if exc.args[0] == "self_action":
            raise ValueError("self_unban") from None
        raise
    if target.banned_at is not None:
        target.banned_at = None
        target.banned_until = None
        target.ban_reason = None
        target.banned_by = None
        _record_action(
            db,
            moderator,
            "user_unbanned",
            target=target,
            resource_type="user",
            previous_state="banned",
            new_state="active",
        )
        db.flush()
    return target


def warn_user(
    db: Session,
    moderator: User,
    target: User,
    *,
    reason: str,
    note: str | None = None,
    report_id: int | None = None,
) -> UserWarning:
    """Issue a user-visible warning and append its immutable audit action."""
    _require_can_enforce(moderator, target)
    reason = _clean_reason(reason)
    if report_id is not None:
        report = db.get(Report, report_id)
        if (
            report is None
            or report.target_user_id != target.id
            or report_expired(report)
        ):
            raise ValueError("invalid_report")
    warning = UserWarning(
        user_id=target.id,
        issued_by=moderator.id,
        report_id=report_id,
        reason=reason,
        note=note,
    )
    db.add(warning)
    db.flush()
    _record_action(
        db,
        moderator,
        "warning_issued",
        target=target,
        reason=reason,
        note=note,
        resource_type="user",
    )
    db.flush()
    return warning


def change_role(
    db: Session,
    admin: User,
    target: User,
    *,
    new_role: str,
    reason: str,
) -> User:
    """Admin-only role assignment; admins cannot mutate self or peer admins."""
    if admin.role != "admin":
        raise ValueError("admin_required")
    if admin.id == target.id:
        raise ValueError("self_role_change")
    if target.role == "admin":
        raise ValueError("insufficient_role")
    if is_actively_banned(target):
        raise ValueError("banned_target")
    if new_role not in ROLES:
        raise ValueError("invalid_role")
    reason = _clean_reason(reason)
    if target.role == new_role:
        return target
    previous = target.role
    target.role = new_role
    _record_action(
        db,
        admin,
        "role_changed",
        target=target,
        reason=reason,
        resource_type="user",
        previous_state=previous,
        new_state=new_role,
    )
    db.flush()
    return target


# ---------------------------------------------------------------------------
# Moderator content removal
# ---------------------------------------------------------------------------

def delete_post_moderation(
    db: Session, moderator: User, post: Post, *, reason: str = "policy_violation",
) -> Post:
    """Delete a post as a moderation action — no author check (the caller
    enforces moderator authorization)."""
    target = db.get(User, post.author_id)
    if target is None:
        raise ValueError("target_missing")
    _require_can_enforce(moderator, target)
    reason = _clean_reason(reason)
    _record_action(
        db, moderator, "post_removed", target=target, reason=reason,
        resource_type="post", resource_id=post.id,
    )
    db.delete(post)
    db.flush()
    return post


def delete_comment_moderation(
    db: Session,
    moderator: User,
    comment: Comment,
    *,
    reason: str = "policy_violation",
) -> Comment:
    """Delete a comment as a moderation action — replies cascade (DB)."""
    target = db.get(User, comment.author_id)
    if target is None:
        raise ValueError("target_missing")
    _require_can_enforce(moderator, target)
    reason = _clean_reason(reason)
    _record_action(
        db, moderator, "comment_removed", target=target, reason=reason,
        resource_type="comment", resource_id=comment.id,
    )
    db.delete(comment)
    db.flush()
    return comment
