import hashlib
import time

import pytest
from bolt11 import Bolt11, MilliSatoshi, TagChar, Tags, decode, encode
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.contracts import InvoiceDecodeError
from app.payments.invoice import parse_invoice

SECRET = "22" * 32
PREIMAGE = "33" * 32
PAYMENT_SECRET = "44" * 32


def signed_invoice(
    *,
    amount_msat: int | None = 4_000,
    date: int | None = None,
    expiry: int | None = None,
    currency: str = "bc",
) -> str:
    tags = Tags()
    tags.add(TagChar.payment_hash, hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest())
    tags.add(TagChar.payment_secret, PAYMENT_SECRET)
    tags.add(TagChar.description, "test invoice")
    tags.add(TagChar.min_final_cltv_expiry, 18)
    if expiry is not None:
        tags.add(TagChar.expire_time, expiry)
    invoice = Bolt11(
        currency=currency,
        date=date if date is not None else int(time.time()),
        amount_msat=MilliSatoshi(amount_msat) if amount_msat is not None else None,
        tags=tags,
    )
    return encode(invoice, private_key=SECRET)


def test_parses_and_signature_checks_valid_invoice():
    now = int(time.time())
    parsed = parse_invoice(signed_invoice(date=now), "bc", now=now)
    assert parsed.network == "bc"
    assert parsed.amount_msat == 4_000
    assert parsed.payment_hash == hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
    assert parsed.created_at == now
    assert parsed.expires_at == now + 3600


def test_rejects_an_invoice_for_the_wrong_network():
    with pytest.raises(InvoiceDecodeError, match="network"):
        parse_invoice(signed_invoice(currency="tb"), "bc")


def test_rejects_amountless_invoice():
    with pytest.raises(InvoiceDecodeError, match="amount"):
        parse_invoice(signed_invoice(amount_msat=None), "bc")


def test_rejects_expired_invoice():
    now = int(time.time())
    with pytest.raises(InvoiceDecodeError, match="expired"):
        parse_invoice(signed_invoice(date=now - 120, expiry=60), "bc", now=now)


def test_invoice_expiry_tag_is_used():
    now = int(time.time())
    parsed = parse_invoice(signed_invoice(date=now, expiry=90), "bc", now=now)
    assert parsed.expires_at == now + 90


def test_omitted_expiry_uses_bolt11_default():
    now = int(time.time())
    parsed = parse_invoice(signed_invoice(date=now), "bc", now=now)
    assert parsed.expires_at == now + 3600


def test_rejects_invalid_signature():
    decoded = decode(signed_invoice())
    assert decoded.signature is not None
    decoded.signature.signature_data = b"\x00" * 65
    invalid = encode(decoded)
    with pytest.raises(InvoiceDecodeError, match="signature"):
        parse_invoice(invalid, "bc")


@pytest.mark.parametrize("invoice", ["", "hello", "lnbc", "lnbc1qqqq"])
def test_rejects_malformed_invoice(invoice):
    with pytest.raises(InvoiceDecodeError):
        parse_invoice(invoice, "bc")


def test_rejects_unknown_expected_network():
    with pytest.raises(InvoiceDecodeError, match="configured network"):
        parse_invoice(signed_invoice(), "fake")


def test_rejects_invoice_expiring_at_exact_current_second():
    now = int(time.time())
    with pytest.raises(InvoiceDecodeError, match="expired"):
        parse_invoice(signed_invoice(date=now - 60, expiry=60), "bc", now=now)


@settings(max_examples=30, deadline=None)
@given(amount_msat=st.integers(min_value=1, max_value=10_000_000_000))
def test_bolt11_amount_round_trips_without_unit_or_precision_loss(amount_msat):
    invoice = signed_invoice(amount_msat=amount_msat)
    parsed = parse_invoice(invoice, "bc")
    assert parsed.amount_msat == amount_msat


def test_rejects_mixed_case_bolt11_invoice():
    invoice = signed_invoice()
    mixed_case = invoice[:5].upper() + invoice[5:]
    with pytest.raises(InvoiceDecodeError, match="mix"):
        parse_invoice(mixed_case, "bc")
