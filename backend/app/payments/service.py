"""Disbursement rules. The API records an organisation's payment attestation; it never pays."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CounsellorAttestation, Disbursement, DisbursementTransition, Organization
from app.db.session import get_engine
from app.directory.service import ts
from app.payments.contracts import ALLOWED_TRANSITIONS, InvoiceDetails
from app.payments.schemas import DisbursementCreate, DisbursementLimits

SYSTEM_ACTOR_PUBKEY = "0" * 64
DAILY_STATES = ("CREATED", "INVOICE_ATTACHED", "PAYING", "PAID")
CREATED_TTL = timedelta(hours=24)


class DisbursementError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def load_org(db: Session, org_id: uuid.UUID, *, lock: bool = False) -> Organization:
    query = select(Organization).where(Organization.id == org_id)
    org = db.scalars(query.with_for_update() if lock else query).one_or_none()
    if org is None:
        raise DisbursementError(404, "organisation not found")
    return org


def can_act_for_org(
    db: Session, org: Organization, actor: str, *, now: datetime | None = None
) -> bool:
    if actor == org.nostr_pubkey:
        return True
    current = now or datetime.now(UTC)
    return (
        db.scalar(
            select(CounsellorAttestation.counsellor_pubkey).where(
                CounsellorAttestation.org_id == org.id,
                CounsellorAttestation.counsellor_pubkey == actor,
                CounsellorAttestation.active.is_(True),
                CounsellorAttestation.expires_at > current,
            )
        )
        is not None
    )


def require_org_actor(db: Session, org: Organization, actor: str) -> None:
    if not can_act_for_org(db, org, actor):
        raise DisbursementError(403, "caller is not the organisation or an active counsellor")


def accessible_org_ids(db: Session, actor: str) -> list[uuid.UUID]:
    org_ids: set[uuid.UUID] = set(
        db.scalars(select(Organization.id).where(Organization.nostr_pubkey == actor)).all()
    )
    org_ids.update(
        db.scalars(
            select(CounsellorAttestation.org_id)
            .join(Organization, Organization.id == CounsellorAttestation.org_id)
            .where(
                CounsellorAttestation.counsellor_pubkey == actor,
                CounsellorAttestation.active.is_(True),
                CounsellorAttestation.expires_at > datetime.now(UTC),
            )
        ).all()
    )
    return sorted(org_ids, key=str)


def request_digest(body: DisbursementCreate) -> str:
    encoded = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _append_transition(
    db: Session, item: Disbursement, actor: str, to_state: str, **changes
) -> Disbursement:
    old = item.state
    if to_state not in ALLOWED_TRANSITIONS.get(old, frozenset()):
        raise DisbursementError(409, f"cannot move disbursement from {old} to {to_state}")
    if actor != SYSTEM_ACTOR_PUBKEY:
        org = db.get(Organization, item.org_id)
        if org is None:
            raise DisbursementError(404, "organisation not found")
        require_org_actor(db, org, actor)

    values = {"state": to_state, "updated_at": func.now(), **changes}
    result = db.execute(
        update(Disbursement)
        .where(Disbursement.id == item.id, Disbursement.state == old)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise DisbursementError(409, "disbursement changed; reload and retry")
    db.add(
        DisbursementTransition(
            disbursement_id=item.id,
            from_state=old,
            to_state=to_state,
            actor_pubkey=actor,
        )
    )
    db.flush()
    db.refresh(item)
    return item


def create_disbursement(
    db: Session, actor: str, body: DisbursementCreate, idempotency_key: str
) -> tuple[Disbursement, bool]:
    org = load_org(db, body.org_id, lock=True)
    if org.status != "approved":
        raise DisbursementError(409, "organisation must be approved to create disbursements")
    require_org_actor(db, org, actor)
    digest = request_digest(body)
    existing = db.scalars(
        select(Disbursement).where(
            Disbursement.org_id == org.id,
            Disbursement.idempotency_key == idempotency_key,
        )
    ).one_or_none()
    if existing is not None:
        if existing.request_hash != digest:
            raise DisbursementError(
                409, "Idempotency-Key was already used with a different request"
            )
        return existing, False

    if org.per_payment_cap_sat <= 0 or org.daily_cap_sat <= 0:
        raise DisbursementError(409, "disbursement limits are not configured for this organisation")
    if body.amount_sat > org.per_payment_cap_sat:
        raise DisbursementError(409, "amount exceeds the per-payment cap")

    now = datetime.now(UTC)
    local_midnight = now.astimezone(ZoneInfo("Africa/Nairobi")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_start_utc = local_midnight.astimezone(UTC)
    reserved = db.scalar(
        select(func.coalesce(func.sum(Disbursement.amount_sat), 0)).where(
            Disbursement.org_id == org.id,
            Disbursement.created_at >= day_start_utc,
            Disbursement.state.in_(DAILY_STATES),
        )
    )
    if int(reserved or 0) + body.amount_sat > org.daily_cap_sat:
        raise DisbursementError(409, "amount would exceed the organisation's daily cap")

    item = Disbursement(
        org_id=org.id,
        idempotency_key=idempotency_key,
        request_hash=digest,
        amount_sat=body.amount_sat,
        amount_kes=body.amount_kes,
        rate_source=body.rate_source.strip(),
        reason_code=body.reason_code,
        state="CREATED",
        created_by_pubkey=actor,
    )
    db.add(item)
    db.flush()
    db.add(
        DisbursementTransition(
            disbursement_id=item.id,
            from_state=None,
            to_state="CREATED",
            actor_pubkey=actor,
        )
    )
    db.flush()
    db.refresh(item)
    return item, True


def get_disbursement(
    db: Session, disbursement_id: uuid.UUID, *, lock: bool = False
) -> Disbursement:
    query = select(Disbursement).where(Disbursement.id == disbursement_id)
    item = db.scalars(query.with_for_update() if lock else query).one_or_none()
    if item is None:
        raise DisbursementError(404, "disbursement not found")
    return item


def require_disbursement_actor(db: Session, item: Disbursement, actor: str) -> Organization:
    org = load_org(db, item.org_id)
    require_org_actor(db, org, actor)
    return org


def attach_invoice(
    db: Session,
    item: Disbursement,
    actor: str,
    invoice: str,
    details: InvoiceDetails,
) -> Disbursement:
    org = require_disbursement_actor(db, item, actor)
    if org.status != "approved":
        raise DisbursementError(409, "organisation must be approved to attach a payment invoice")
    if item.state in {"INVOICE_ATTACHED", "PAYING"} and item.invoice == invoice:
        return item
    if details.amount_msat != item.amount_sat * 1000:
        raise DisbursementError(409, "invoice amount must exactly match amount_sat")
    if item.payment_hash == details.payment_hash and item.state == "FAILED":
        # A failed payment's expired invoice cannot be reused, even if the payment hash matches.
        raise DisbursementError(409, "retry requires a new invoice")
    conflict = db.scalars(
        select(Disbursement.id).where(
            Disbursement.payment_hash == details.payment_hash,
            Disbursement.id != item.id,
        )
    ).first()
    if conflict is not None:
        raise DisbursementError(409, "invoice payment hash has already been used")
    expires_at = ts(details.expires_at)
    try:
        return _append_transition(
            db,
            item,
            actor,
            "INVOICE_ATTACHED",
            invoice=invoice,
            payment_hash=details.payment_hash,
            invoice_expires_at=expires_at,
            paid_at=None,
        )
    except IntegrityError as exc:
        raise DisbursementError(409, "invoice payment hash has already been used") from exc


def mark_paying(db: Session, item: Disbursement, actor: str) -> Disbursement:
    org = require_disbursement_actor(db, item, actor)
    if org.status != "approved":
        raise DisbursementError(409, "organisation must be approved to start a payment")
    if item.state == "PAYING":
        return item
    if item.state != "INVOICE_ATTACHED":
        raise DisbursementError(409, "only an attached invoice can enter PAYING")
    if item.invoice_expires_at is None or item.invoice_expires_at <= datetime.now(UTC):
        raise DisbursementError(409, "invoice has expired")
    return _append_transition(db, item, actor, "PAYING")


def submit_preimage(db: Session, item: Disbursement, actor: str, preimage: str) -> Disbursement:
    require_disbursement_actor(db, item, actor)
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if item.payment_hash != payment_hash:
        raise DisbursementError(409, "preimage does not match this invoice payment hash")
    if item.state == "PAID":
        return item
    if item.state != "PAYING":
        raise DisbursementError(409, "payment must be marked PAYING before submitting proof")
    return _append_transition(
        db,
        item,
        actor,
        "PAID",
        invoice=None,
        paid_at=datetime.now(UTC),
    )


def cancel_disbursement(db: Session, item: Disbursement, actor: str) -> Disbursement:
    require_disbursement_actor(db, item, actor)
    if item.state == "CANCELLED":
        return item
    if item.state not in {"CREATED", "INVOICE_ATTACHED", "FAILED"}:
        raise DisbursementError(409, f"cannot cancel a disbursement in {item.state}")
    return _append_transition(db, item, actor, "CANCELLED", invoice=None)


def set_limits(db: Session, org_id: uuid.UUID, limits: DisbursementLimits) -> Organization:
    org = load_org(db, org_id, lock=True)
    org.per_payment_cap_sat = limits.per_payment_cap_sat
    org.daily_cap_sat = limits.daily_cap_sat
    db.flush()
    return org


def list_disbursements(db: Session, actor: str, state: str | None = None) -> list[Disbursement]:
    org_ids = accessible_org_ids(db, actor)
    if not org_ids:
        raise DisbursementError(403, "caller is not an organisation or active counsellor")
    query = select(Disbursement).where(Disbursement.org_id.in_(org_ids))
    if state is not None:
        query = query.where(Disbursement.state == state)
    return list(db.scalars(query.order_by(Disbursement.created_at.desc()).limit(100)).all())


def expire_disbursements(now: datetime | None = None) -> dict[str, int]:
    current = now or datetime.now(UTC)
    counts = {"expired": 0}
    with Session(get_engine()) as db:
        due = db.scalars(
            select(Disbursement)
            .where(
                or_(
                    and_(
                        Disbursement.state == "CREATED",
                        Disbursement.created_at < current - CREATED_TTL,
                    ),
                    and_(
                        Disbursement.state == "INVOICE_ATTACHED",
                        Disbursement.invoice_expires_at <= current,
                    ),
                )
            )
            .with_for_update(skip_locked=True)
        ).all()
        for item in due:
            target = "EXPIRED"
            counts["expired"] += 1
            _append_transition(
                db,
                item,
                SYSTEM_ACTOR_PUBKEY,
                target,
                invoice=None,
            )
        db.commit()
    return counts
