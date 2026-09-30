"""A test-only endpoint that simulates a wallet reporting settlement."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.nip98 import NostrPubkey
from app.db.session import get_db
from app.payments.schemas import DisbursementOut, PaymentProof
from app.payments.service import (
    DisbursementError,
    get_disbursement,
    mark_paying,
    submit_preimage,
)
from app.settings import Settings, get_settings

router = APIRouter(prefix="/v1/_mock", tags=["test mock only"])
Db = Annotated[Session, Depends(get_db)]


@router.post("/settle/{disbursement_id}", response_model=DisbursementOut)
def mock_settle(
    disbursement_id: uuid.UUID,
    body: PaymentProof,
    actor: NostrPubkey,
    db: Db,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DisbursementOut:
    if settings.app_env != "test":
        raise HTTPException(404, "not found")
    try:
        item = get_disbursement(db, disbursement_id, lock=True)
        if item.state == "INVOICE_ATTACHED":
            item = mark_paying(db, item, actor)
        item = submit_preimage(db, item, actor, body.preimage)
        db.commit()
        db.refresh(item)
        return DisbursementOut.model_validate(item)
    except (DisbursementError, IntegrityError) as exc:
        db.rollback()
        if isinstance(exc, DisbursementError):
            raise HTTPException(exc.status_code, exc.detail) from exc
        raise HTTPException(409, "request conflicts with an existing disbursement") from exc
