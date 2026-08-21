"""Account lifecycle endpoints — data export, self-deactivation (T-023).

* GET  /api/v1/account/export      — download everything the account owns
* POST /api/v1/account/deactivate  — reversible pause (password-confirmed)
* POST /api/v1/account/reactivate  — lift the pause
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

