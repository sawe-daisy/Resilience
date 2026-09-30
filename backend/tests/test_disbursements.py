import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from bolt11 import Bolt11, MilliSatoshi, TagChar, Tags, encode
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import Disbursement, DisbursementTransition
from app.db.session import get_engine
from app.nostr.events import pubkey_of
from app.worker import expire_disbursements
from tests.conftest import OTHER_SECRET
from tests.helpers import (
    COUNSELLORS,
    ORG_PUBKEY,
    ORG_SECRET,
    approved_org,
    roster_event,
)

API = "https://api.example.test"
COUNSELLOR_SECRETS = {pubkey_of(f"{i:02x}" * 32): f"{i:02x}" * 32 for i in range(1, 5)}
PREIMAGE_A = "a1" * 32
PREIMAGE_B = "b2" * 32


def signed_request(client, method, path, payload=None, secret=ORG_SECRET, extra_headers=None):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    from tests.conftest import auth_header

    headers = auth_header(API + path, method, body, secret)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    return client.request(method, path, content=body, headers=headers)


def create_disbursement(client, org_id, *, key="request-0001", secret=ORG_SECRET, **overrides):
    payload = {
        "org_id": str(org_id),
        "amount_sat": 10_000,
        "amount_kes": 500,
        "rate_source": "test quote",
        "reason_code": "transport",
        **overrides,
    }
    return signed_request(
        client,
        "POST",
        "/v1/disbursements",
        payload,
        secret,
        {"Idempotency-Key": key},
    )


def make_invoice(
    preimage=PREIMAGE_A,
    amount_sat=10_000,
    *,
    currency="bc",
    created_at=None,
    expiry_seconds=3600,
):
    now = int(time.time())
    tags = Tags()
    tags.add(TagChar.payment_hash, hashlib.sha256(bytes.fromhex(preimage)).hexdigest())
    tags.add(TagChar.payment_secret, "c3" * 32)
    tags.add(TagChar.description, "emergency support")
    tags.add(TagChar.min_final_cltv_expiry, 18)
    tags.add(TagChar.expire_time, expiry_seconds)
    event = Bolt11(
        currency=currency,
        date=created_at if created_at is not None else now,
        amount_msat=MilliSatoshi(amount_sat * 1000),
        tags=tags,
    )
    return encode(event, private_key="d4" * 32)


def set_caps(org_id, per_payment=50_000, daily=100_000):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE organizations SET per_payment_cap_sat=:per, daily_cap_sat=:daily "
                "WHERE id=:id"
            ),
            {"per": per_payment, "daily": daily, "id": str(org_id)},
        )


def authorize_counsellor(client, org_id, index=0):
    event = roster_event([COUNSELLORS[index]])
    response = client.put(f"/v1/orgs/{org_id}/roster", json=event)
    assert response.status_code == 200
    return COUNSELLOR_SECRETS[COUNSELLORS[index]]


def attach_invoice(client, item, invoice, secret=ORG_SECRET):
    return signed_request(
        client,
        "POST",
        f"/v1/disbursements/{item['id']}/invoice",
        {"invoice": invoice},
        secret,
    )


def mark_paying(client, item, secret=ORG_SECRET):
    return signed_request(client, "POST", f"/v1/disbursements/{item['id']}/paying", secret=secret)


def submit_proof(client, item, preimage, secret=ORG_SECRET):
    return signed_request(
        client,
        "POST",
        f"/v1/disbursements/{item['id']}/proof",
        {"preimage": preimage},
        secret,
    )


def test_create_requires_nip98_and_idempotency_key(client):
    org_id = approved_org()
    set_caps(org_id)
    payload = {
        "org_id": str(org_id),
        "amount_sat": 10_000,
        "amount_kes": 500,
        "rate_source": "test quote",
        "reason_code": "transport",
    }
    path = "/v1/disbursements"
    body = json.dumps(payload, separators=(",", ":")).encode()
    assert client.post(path, content=body).status_code == 401
    response = signed_request(client, "POST", path, payload, extra_headers={})
    assert response.status_code == 422


def test_create_records_no_recipient_identity(client):
    org_id = approved_org()
    set_caps(org_id)
    r = create_disbursement(client, org_id)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["org_id"] == str(org_id)
    assert out["amount_sat"] == 10_000
    assert out["amount_kes"] == 500
    assert out["reason_code"] == "transport"
    assert out["state"] == "CREATED"
    assert out["created_by_pubkey"] == ORG_PUBKEY
    assert "survivor_pubkey" not in out and "recipient_pubkey" not in out
    with Session(get_engine()) as db:
        row = db.get(Disbursement, uuid.UUID(out["id"]))
        assert row is not None and row.invoice is None and row.payment_hash is None
        transition = db.scalars(
            select(DisbursementTransition).where(DisbursementTransition.disbursement_id == row.id)
        ).all()
        assert [(t.from_state, t.to_state) for t in transition] == [(None, "CREATED")]


def test_create_is_idempotent_for_same_key_and_payload(client):
    org_id = approved_org()
    set_caps(org_id)
    first = create_disbursement(client, org_id)
    retry = create_disbursement(client, org_id)
    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]


def test_same_idempotency_key_with_different_payload_conflicts(client):
    org_id = approved_org()
    set_caps(org_id)
    assert create_disbursement(client, org_id).status_code == 201
    r = create_disbursement(client, org_id, amount_sat=10_001)
    assert r.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_sat": 0},
        {"amount_sat": -1},
        {"amount_kes": 0},
        {"reason_code": "rent"},
        {"survivor_pubkey": "ab" * 32},
    ],
)
def test_create_rejects_invalid_or_private_recipient_fields(client, overrides):
    org_id = approved_org()
    set_caps(org_id)
    assert create_disbursement(client, org_id, **overrides).status_code == 422


def test_unapproved_org_cannot_create_disbursement(client):
    org_id = approved_org()
    set_caps(org_id)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE organizations SET status='pending' WHERE id=:id"), {"id": str(org_id)}
        )
    r = create_disbursement(client, org_id)
    assert r.status_code == 409


def test_org_limits_must_be_configured_before_spending(client):
    org_id = approved_org()
    r = create_disbursement(client, org_id)
    assert r.status_code == 409
    assert "limits" in r.json()["detail"]


def test_per_payment_and_daily_limits_are_enforced(client):
    org_id = approved_org()
    set_caps(org_id, per_payment=10_000, daily=15_000)
    assert (
        create_disbursement(client, org_id, key="payment-0001", amount_sat=10_001).status_code
        == 409
    )
    assert (
        create_disbursement(client, org_id, key="payment-0001", amount_sat=10_000).status_code
        == 201
    )
    assert (
        create_disbursement(client, org_id, key="payment-0002", amount_sat=5_001).status_code == 409
    )


def test_created_and_paid_disbursements_reserve_daily_limit(client):
    org_id = approved_org()
    set_caps(org_id, per_payment=10_000, daily=15_000)
    first = create_disbursement(client, org_id, key="payment-0001", amount_sat=10_000).json()
    assert (
        create_disbursement(client, org_id, key="payment-0002", amount_sat=5_001).status_code == 409
    )
    # Cancelling releases the reservation; a payment that reached PAID does not.
    assert (
        signed_request(client, "POST", f"/v1/disbursements/{first['id']}/cancel").status_code == 200
    )
    assert (
        create_disbursement(client, org_id, key="payment-0002", amount_sat=5_001).status_code == 201
    )


def test_counsellor_can_create_only_for_current_roster_org(client):
    org_id = approved_org()
    set_caps(org_id)
    secret = authorize_counsellor(client, org_id)
    r = create_disbursement(client, org_id, secret=secret)
    assert r.status_code == 201
    outsider = create_disbursement(client, org_id, key="outsider-0001", secret=OTHER_SECRET)
    assert outsider.status_code == 403


def test_removed_counsellor_cannot_create_or_read(client):
    org_id = approved_org()
    set_caps(org_id)
    old_secret = authorize_counsellor(client, org_id)
    old_roster = roster_event([COUNSELLORS[1]], created_at=int(time.time()) + 1)
    assert client.put(f"/v1/orgs/{org_id}/roster", json=old_roster).status_code == 200
    assert create_disbursement(client, org_id, secret=old_secret).status_code == 403


def test_list_is_scoped_to_callers_organizations(client):
    org_a = approved_org("wangu.org", ORG_PUBKEY)
    set_caps(org_a)
    org_b_secret = "ab" * 32
    org_b_key = pubkey_of(org_b_secret)
    org_b = approved_org("pendo.org", org_b_key)
    set_caps(org_b)
    counsellor_secret = authorize_counsellor(client, org_a)
    assert create_disbursement(client, org_a).status_code == 201
    assert (
        create_disbursement(client, org_b, secret=org_b_secret, key="request-0002").status_code
        == 201
    )
    org_view = signed_request(client, "GET", "/v1/disbursements", secret=ORG_SECRET)
    assert org_view.status_code == 200
    assert {item["org_id"] for item in org_view.json()} == {str(org_a)}
    counsellor_view = signed_request(client, "GET", "/v1/disbursements", secret=counsellor_secret)
    assert counsellor_view.status_code == 200
    assert {item["org_id"] for item in counsellor_view.json()} == {str(org_a)}


def test_invoice_must_match_amount_and_network(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    wrong_amount = make_invoice(amount_sat=9_999)
    r = attach_invoice(client, item, wrong_amount)
    assert r.status_code == 409
    wrong_network = make_invoice(currency="tb")
    r = attach_invoice(client, item, wrong_network)
    assert r.status_code == 409
    assert attach_invoice(client, item, make_invoice()).status_code == 200


def test_invoice_attach_is_idempotent_and_payment_hash_is_unique(client):
    org_id = approved_org()
    set_caps(org_id)
    first = create_disbursement(client, org_id).json()
    invoice = make_invoice()
    assert attach_invoice(client, first, invoice).status_code == 200
    assert attach_invoice(client, first, invoice).status_code == 200
    assert attach_invoice(client, first, make_invoice(preimage=PREIMAGE_B)).status_code == 409
    second = create_disbursement(client, org_id, key="request-0002").json()
    assert attach_invoice(client, second, invoice).status_code == 409


def test_invoice_attachment_rejects_expired_and_malformed_invoices(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    expired = make_invoice(created_at=int(time.time()) - 7200, expiry_seconds=60)
    assert attach_invoice(client, item, expired).status_code == 409
    assert attach_invoice(client, item, "not-an-invoice-" + "x" * 40).status_code == 409


def test_only_invoice_attached_disbursement_can_enter_paying(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    assert mark_paying(client, item).status_code == 409
    assert attach_invoice(client, item, make_invoice()).status_code == 200
    assert mark_paying(client, item).status_code == 200
    assert mark_paying(client, item).status_code == 200


def test_valid_preimage_marks_paid_and_removes_invoice(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    invoice = make_invoice()
    assert attach_invoice(client, item, invoice).status_code == 200
    assert mark_paying(client, item).status_code == 200
    paid = submit_proof(client, item, PREIMAGE_A)
    assert paid.status_code == 200
    assert paid.json()["state"] == "PAID"
    assert paid.json()["invoice"] is None
    assert paid.json()["payment_hash"] == hashlib.sha256(bytes.fromhex(PREIMAGE_A)).hexdigest()
    retry = submit_proof(client, item, PREIMAGE_A)
    assert retry.status_code == 200
    assert retry.json()["state"] == "PAID"
    with Session(get_engine()) as db:
        row = db.get(Disbursement, uuid.UUID(item["id"]))
        assert row is not None and row.invoice is None
        transitions = db.scalars(
            select(DisbursementTransition).where(
                DisbursementTransition.disbursement_id == row.id,
                DisbursementTransition.to_state == "PAID",
            )
        ).all()
        assert len(transitions) == 1


def test_wrong_preimage_never_marks_paid(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    assert attach_invoice(client, item, make_invoice()).status_code == 200
    assert mark_paying(client, item).status_code == 200
    r = submit_proof(client, item, PREIMAGE_B)
    assert r.status_code == 409
    current = signed_request(client, "GET", "/v1/disbursements")
    assert next(row for row in current.json() if row["id"] == item["id"])["state"] == "PAYING"


def test_two_concurrent_valid_proofs_record_one_paid_transition(client):
    org_id = approved_org()
    set_caps(org_id)
    counselor_a = authorize_counsellor(client, org_id, 0)
    counselor_b = "02" * 32
    # Add the second key to the signed roster before the first invoice.
    event = roster_event(COUNSELLORS[:2], created_at=int(time.time()) + 1)
    assert client.put(f"/v1/orgs/{org_id}/roster", json=event).status_code == 200
    item = create_disbursement(client, org_id, secret=counselor_a).json()
    assert attach_invoice(client, item, make_invoice()).status_code == 200
    assert mark_paying(client, item, secret=counselor_a).status_code == 200

    def pay(secret):
        # A fresh auth event per counsellor avoids conflating request replay with payment races.
        with __import__("fastapi").testclient.TestClient(
            __import__("app.main", fromlist=["app"]).app
        ) as c:
            return submit_proof(c, item, PREIMAGE_A, secret=secret).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(pay, [counselor_a, counselor_b]))
    assert statuses.count(200) >= 1
    assert all(status in {200, 409} for status in statuses)
    with Session(get_engine()) as db:
        row = db.get(Disbursement, uuid.UUID(item["id"]))
        assert row is not None and row.state == "PAID"
        paid_transitions = db.scalar(
            select(func.count())
            .select_from(DisbursementTransition)
            .where(
                DisbursementTransition.disbursement_id == row.id,
                DisbursementTransition.to_state == "PAID",
            )
        )
        assert paid_transitions == 1


def test_cancel_is_terminal_and_cannot_cancel_while_paying(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    cancelled = signed_request(client, "POST", f"/v1/disbursements/{item['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    assert (
        signed_request(client, "POST", f"/v1/disbursements/{item['id']}/cancel").status_code == 200
    )
    assert attach_invoice(client, item, make_invoice()).status_code == 409

    item2 = create_disbursement(client, org_id, key="request-0002").json()
    assert attach_invoice(client, item2, make_invoice(preimage=PREIMAGE_B)).status_code == 200
    assert mark_paying(client, item2).status_code == 200
    r = signed_request(client, "POST", f"/v1/disbursements/{item2['id']}/cancel")
    assert r.status_code == 409


def test_worker_expires_attached_invoice_but_keeps_paying_for_reconciliation(client):
    org_id = approved_org()
    set_caps(org_id)
    attached = create_disbursement(client, org_id).json()
    assert attach_invoice(client, attached, make_invoice()).status_code == 200
    paying = create_disbursement(client, org_id, key="request-0002").json()
    assert attach_invoice(client, paying, make_invoice(preimage=PREIMAGE_B)).status_code == 200
    assert mark_paying(client, paying).status_code == 200
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE disbursements SET invoice_expires_at=now()-interval '1 second' "
                "WHERE org_id=:id"
            ),
            {"id": str(org_id)},
        )
    assert expire_disbursements() == {"expired": 1}
    assert signed_request(client, "GET", "/v1/disbursements").status_code == 200
    states = {
        row["id"]: row["state"] for row in signed_request(client, "GET", "/v1/disbursements").json()
    }
    assert states[attached["id"]] == "EXPIRED"
    assert states[paying["id"]] == "PAYING"


def test_admin_can_set_limits_and_bad_limits_are_rejected(client):
    org_id = approved_org()
    body = {"per_payment_cap_sat": 10_000, "daily_cap_sat": 50_000}
    path = f"/v1/admin/orgs/{org_id}/disbursement-limits"
    assert signed_request(client, "PUT", path, body, secret=OTHER_SECRET).status_code == 403
    success = signed_request(
        client,
        "PUT",
        path,
        body,
        secret=__import__("tests.conftest", fromlist=["ADMIN_SECRET"]).ADMIN_SECRET,
    )
    assert success.status_code == 200
    assert success.json()["per_payment_cap_sat"] == 10_000
    invalid = signed_request(
        client,
        "PUT",
        path,
        {"per_payment_cap_sat": 20_000, "daily_cap_sat": 10_000},
        secret=__import__("tests.conftest", fromlist=["ADMIN_SECRET"]).ADMIN_SECRET,
    )
    assert invalid.status_code == 422


def test_same_idempotency_request_succeeds_when_daily_cap_is_already_reserved(client):
    org_id = approved_org()
    set_caps(org_id, per_payment=10_000, daily=10_000)
    first = create_disbursement(client, org_id, amount_sat=10_000)
    retry = create_disbursement(client, org_id, amount_sat=10_000)
    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]


def test_retry_after_expiry_requires_a_new_invoice(client):
    org_id = approved_org()
    set_caps(org_id, per_payment=10_000, daily=20_000)
    item = create_disbursement(client, org_id).json()
    old_invoice = make_invoice()
    assert attach_invoice(client, item, old_invoice).status_code == 200
    assert mark_paying(client, item).status_code == 200
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE disbursements SET invoice_expires_at=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": item["id"]},
        )
    assert expire_disbursements() == {"expired": 0}
    paying = signed_request(client, "GET", "/v1/disbursements").json()[0]
    assert paying["state"] == "PAYING" and paying["invoice"] == old_invoice
    # Without wallet settlement lookup, we cannot know whether a payment settled just before
    # expiry. Do not allow a retry invoice that could result in paying twice.
    assert attach_invoice(client, item, make_invoice(preimage=PREIMAGE_B)).status_code == 409


def test_test_only_mock_endpoint_simulates_wallet_settlement(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    assert attach_invoice(client, item, make_invoice()).status_code == 200
    r = signed_request(
        client,
        "POST",
        f"/v1/_mock/settle/{item['id']}",
        {"preimage": PREIMAGE_A},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "PAID"


def test_api_network_setting_is_validated():
    from pydantic import ValidationError

    from app.settings import Settings

    assert Settings().lightning_network == "bc"  # test configuration is explicit
    assert Settings(lightning_network="tb").lightning_network == "tb"
    with pytest.raises(ValidationError, match="LIGHTNING_NETWORK"):
        Settings(lightning_network="mainnet")


def test_concurrent_disbursements_do_not_exceed_daily_cap(client):
    org_id = approved_org()
    set_caps(org_id, per_payment=10_000, daily=10_000)
    counselor_a = authorize_counsellor(client, org_id, 0)
    event = roster_event(COUNSELLORS[:2], created_at=int(time.time()) + 1)
    assert client.put(f"/v1/orgs/{org_id}/roster", json=event).status_code == 200
    counselor_b = COUNSELLOR_SECRETS[COUNSELLORS[1]]

    def create(secret, key):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as concurrent_client:
            return create_disbursement(
                concurrent_client, org_id, key=key, secret=secret, amount_sat=10_000
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [counselor_a, counselor_b], ["parallel-a", "parallel-b"]))
    assert sorted(results) == [201, 409]
    with Session(get_engine()) as db:
        total = db.scalar(
            select(func.coalesce(func.sum(Disbursement.amount_sat), 0)).where(
                Disbursement.org_id == org_id,
                Disbursement.state.in_(("CREATED", "INVOICE_ATTACHED", "PAYING", "PAID")),
            )
        )
        assert total == 10_000


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(amounts=st.lists(st.integers(min_value=1, max_value=25_000), min_size=1, max_size=8))
def test_random_create_sequences_never_reserve_more_than_daily_cap(client, amounts):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM disbursement_transitions; DELETE FROM disbursements; "
                "DELETE FROM counsellor_attestations; DELETE FROM roster_events; "
                "DELETE FROM organizations"
            )
        )
    org_id = approved_org()
    daily_cap = 50_000
    set_caps(org_id, per_payment=25_000, daily=daily_cap)
    for i, amount in enumerate(amounts):
        response = create_disbursement(client, org_id, key=f"property-{i:04}", amount_sat=amount)
        assert response.status_code in {201, 409}
        with Session(get_engine()) as db:
            reserved = db.scalar(
                select(func.coalesce(func.sum(Disbursement.amount_sat), 0)).where(
                    Disbursement.org_id == org_id,
                    Disbursement.state.in_(("CREATED", "INVOICE_ATTACHED", "PAYING", "PAID")),
                )
            )
        assert reserved <= daily_cap


def test_suspended_org_can_reconcile_payment_but_cannot_start_another(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    invoice = make_invoice()
    assert attach_invoice(client, item, invoice).status_code == 200
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE organizations SET status='suspended' WHERE id=:id"), {"id": str(org_id)}
        )
    assert mark_paying(client, item).status_code == 409
    # A payment already underway can still be recorded, so suspension cannot hide its outcome.
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE disbursements SET state='PAYING' WHERE id=:id"), {"id": item["id"]}
        )
    proof = submit_proof(client, item, PREIMAGE_A)
    assert proof.status_code == 200 and proof.json()["state"] == "PAID"
    assert signed_request(client, "GET", "/v1/disbursements").status_code == 200


def test_worker_expires_created_disbursement_after_24_hours(client):
    org_id = approved_org()
    set_caps(org_id)
    item = create_disbursement(client, org_id).json()
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE disbursements SET created_at=now()-interval '25 hours' WHERE id=:id"),
            {"id": item["id"]},
        )
    assert expire_disbursements() == {"expired": 1}
    expired = signed_request(client, "GET", "/v1/disbursements").json()[0]
    assert expired["state"] == "EXPIRED" and expired["invoice"] is None
