"""Account lifecycle endpoints — export, self-deactivation, deletion (T-023).

* GET  /api/v1/account/export      — download everything the account owns
* POST /api/v1/account/deactivate  — reversible pause (password-confirmed)
* POST /api/v1/account/reactivate  — lift the pause
* POST /api/v1/account/delete      — permanent deletion (password-confirmed)
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ting_ting import account as account_service
from ting_ting.auth import clear_auth_cookie, clear_refresh_cookie, get_current_user
from ting_ting.database import get_db
from ting_ting.models import User

router = APIRouter(prefix="/account", tags=["account"])


class DeactivateRequest(BaseModel):
    password: str = Field(min_length=1)


class DeleteRequest(BaseModel):
    password: str = Field(min_length=1)


@router.get("/export")
def export_my_data(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Return the caller's full account document (their data only)."""
    return account_service.export_account(db, me)


@router.post("/deactivate", status_code=status.HTTP_200_OK)
def deactivate(
    body: DeactivateRequest,
    response: Response,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Reversibly deactivate the account (hides it from everyone else).

    Requires the current password. All sessions are revoked immediately;
    sign-in stays possible so the account can be reactivated later.
    """
    try:
        account_service.deactivate_account(db, me, body.password)
    except ValueError as exc:
        if exc.args[0] == "invalid_password":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "unauthenticated", "message": "Current password is incorrect."},
            ) from None
        raise

    db.commit()
    clear_auth_cookie(response)
    clear_refresh_cookie(response)
    return {
        "message": "Account deactivated. "
        "Sign in and reactivate whenever you are ready to come back.",
        "deactivated": True,
    }


@router.post("/reactivate", status_code=status.HTTP_200_OK)
def reactivate(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Lift a self-deactivation — the account becomes visible again."""
    was = me.deactivated_at is not None
    account_service.reactivate_account(db, me)
    db.commit()
    return {"message": "Account reactivated." if was else "Account was already active.",
            "deactivated": False}


@router.post("/delete", status_code=status.HTTP_200_OK)
def delete(
    body: DeleteRequest,
    response: Response,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Permanently delete the account and all of its content.

    Requires the current password. Content, sessions, and graph rows are
    removed; moderation reports that referenced the account are kept
    anonymized for the evidence-retention window. The username and email
    stay reserved for 30 days (deletion tombstone). Irreversible.
    """
    from ting_ting.media import delete_stored_file

    try:
        media_paths = account_service.delete_account(db, me, body.password)
    except ValueError as exc:
        if exc.args[0] == "invalid_password":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "unauthenticated", "message": "Current password is incorrect."},
            ) from None
        raise

    db.commit()
    # Files after commit — a failed unlink leaves a reclaimable orphan, never
    # a live row pointing at a deleted file.
    for path in media_paths:
        delete_stored_file(path)
    clear_auth_cookie(response)
    clear_refresh_cookie(response)
    return {
        "message": "Your account and all of its content have been permanently "
                   "deleted. Your username and email stay reserved for 30 days.",
        "deleted": True,
    }
