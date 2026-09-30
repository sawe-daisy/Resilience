"""Platform admin actions. Every call is NIP-98 signed by a key listed in ADMIN_PUBKEYS."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.admin import AdminPubkey
from app.db.models import Organization
from app.db.session import get_db
from app.directory.nip05 import check_nip05
from app.directory.schemas import OrgOut
from app.directory.service import Nip05Fetcher, get_org
from app.payments.schemas import DisbursementLimits, DisbursementLimitsOut
from app.payments.service import DisbursementError, set_limits

router = APIRouter(prefix="/v1/admin/orgs", tags=["admin"])
Db = Annotated[Session, Depends(get_db)]


@router.get("")
def list_orgs(
    _admin: AdminPubkey, db: Db, status: Literal["pending", "approved", "suspended"] = "pending"
) -> list[OrgOut]:
    orgs = db.scalars(
        select(Organization).where(Organization.status == status).order_by(Organization.created_at)
    ).all()
    return [OrgOut.of(org) for org in orgs]


@router.post("/{org_id}/approve")
def approve(org_id: uuid.UUID, _admin: AdminPubkey, db: Db, fetch: Nip05Fetcher) -> OrgOut:
    """Approves only if the organisation's website vouches for its key right now."""
    org = get_org(db, org_id, lock=True)
    result = check_nip05(org.domain, org.nostr_pubkey, fetch)
    if not result.ok:
        db.rollback()
        raise HTTPException(409, f"NIP-05 check failed: {result.reason}")
    org.status = "approved"
    org.nip05_verified_at = datetime.now(UTC)
    db.commit()
    return OrgOut.of(org)


@router.post("/{org_id}/suspend")
def suspend(org_id: uuid.UUID, _admin: AdminPubkey, db: Db) -> OrgOut:
    org = get_org(db, org_id, lock=True)
    org.status = "suspended"
    db.commit()
    return OrgOut.of(org)


@router.put("/{org_id}/disbursement-limits")
def update_disbursement_limits(
    org_id: uuid.UUID,
    limits: DisbursementLimits,
    _admin: AdminPubkey,
    db: Db,
) -> DisbursementLimitsOut:
    try:
        org = set_limits(db, org_id, limits)
        db.commit()
        db.refresh(org)
        return DisbursementLimitsOut(
            org_id=org.id,
            per_payment_cap_sat=org.per_payment_cap_sat,
            daily_cap_sat=org.daily_cap_sat,
        )
    except DisbursementError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
