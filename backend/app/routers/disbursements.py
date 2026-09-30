"""Non-custodial emergency disbursement records."""

import uuid
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.nip98 import NostrPubkey
from app.db.session import get_db
from app.payments.contracts import InvoiceDecodeError
from app.payments.invoice import parse_invoice
from app.payments.schemas import DisbursementCreate, DisbursementOut, InvoiceAttach, PaymentProof
from app.payments.service import (
    DisbursementError,
    cancel_disbursement,
    create_disbursement,
    get_disbursement,
    list_disbursements,
    mark_paying,
    submit_preimage,
)
from app.payments.service import (
    attach_invoice as service_attach_invoice,
)
from app.settings import Settings, get_settings

router = APIRouter(prefix="/v1/disbursements", tags=["disbursements"])
Db = Annotated[Session, Depends(get_db)]


def fail(db: Session, exc: Exception) -> NoReturn:
    db.rollback()
    if isinstance(exc, DisbursementError):
        raise HTTPException(exc.status_code, exc.detail) from exc
    if isinstance(exc, InvoiceDecodeError):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(409, "request conflicts with an existing disbursement") from exc
    raise exc


@router.post("", response_model=DisbursementOut)
def create(
    body: DisbursementCreate,
    response: Response,
    actor: NostrPubkey,
    db: Db,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9._~-]+$",
        ),
    ],
) -> DisbursementOut:
    try:
        item, created = create_disbursement(db, actor, body, idempotency_key)
        db.commit()
        db.refresh(item)
    except (DisbursementError, IntegrityError) as exc:
        fail(db, exc)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return DisbursementOut.model_validate(item)


@router.get("", response_model=list[DisbursementOut])
def list_items(
    actor: NostrPubkey,
    db: Db,
    state: Literal[
        "CREATED", "INVOICE_ATTACHED", "PAYING", "PAID", "EXPIRED", "FAILED", "CANCELLED"
    ]
    | None = None,
) -> list[DisbursementOut]:
    try:
        return [DisbursementOut.model_validate(row) for row in list_disbursements(db, actor, state)]
    except DisbursementError as exc:
        fail(db, exc)


@router.post("/{disbursement_id}/invoice", response_model=DisbursementOut)
def add_invoice(
    disbursement_id: uuid.UUID,
    body: InvoiceAttach,
    actor: NostrPubkey,
    db: Db,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DisbursementOut:
    try:
        item = get_disbursement(db, disbursement_id, lock=True)
        details = parse_invoice(body.invoice, settings.lightning_network)
        item = service_attach_invoice(db, item, actor, body.invoice, details)
        db.commit()
        db.refresh(item)
        return DisbursementOut.model_validate(item)
    except (DisbursementError, InvoiceDecodeError, IntegrityError) as exc:
        fail(db, exc)


@router.post("/{disbursement_id}/paying", response_model=DisbursementOut)
def begin_payment(disbursement_id: uuid.UUID, actor: NostrPubkey, db: Db) -> DisbursementOut:
    try:
        item = get_disbursement(db, disbursement_id, lock=True)
        item = mark_paying(db, item, actor)
        db.commit()
        db.refresh(item)
        return DisbursementOut.model_validate(item)
    except (DisbursementError, IntegrityError) as exc:
        fail(db, exc)


@router.post("/{disbursement_id}/proof", response_model=DisbursementOut)
def payment_proof(
    disbursement_id: uuid.UUID, body: PaymentProof, actor: NostrPubkey, db: Db
) -> DisbursementOut:
    try:
        item = get_disbursement(db, disbursement_id, lock=True)
        item = submit_preimage(db, item, actor, body.preimage)
        db.commit()
        db.refresh(item)
        return DisbursementOut.model_validate(item)
    except (DisbursementError, IntegrityError) as exc:
        fail(db, exc)


@router.post("/{disbursement_id}/cancel", response_model=DisbursementOut)
def cancel(disbursement_id: uuid.UUID, actor: NostrPubkey, db: Db) -> DisbursementOut:
    try:
        item = get_disbursement(db, disbursement_id, lock=True)
        item = cancel_disbursement(db, item, actor)
        db.commit()
        db.refresh(item)
        return DisbursementOut.model_validate(item)
    except (DisbursementError, IntegrityError) as exc:
        fail(db, exc)
