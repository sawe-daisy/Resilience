"""Parse and verify a BOLT11 invoice before attaching it to a disbursement."""

import time

from bolt11 import decode

from app.payments.contracts import InvoiceDecodeError, InvoiceDetails

SUPPORTED_NETWORKS = frozenset({"bc", "tb", "bcrt", "tbs"})
MAX_INVOICE_LENGTH = 4096


def parse_invoice(invoice: str, expected_network: str, now: int | None = None) -> InvoiceDetails:
    """Decode a signed Lightning invoice and return its verified payment terms.

    The library verifies the BOLT11 signature. This checks invoice authenticity and fields; it
    does not make claims about whether an invoice has been paid.
    """
    if expected_network not in SUPPORTED_NETWORKS:
        raise InvoiceDecodeError("unsupported configured network")
    if not isinstance(invoice, str) or not invoice or len(invoice) > MAX_INVOICE_LENGTH:
        raise InvoiceDecodeError(
            "invoice must be a non-empty BOLT11 string no longer than 4096 characters"
        )
    # BOLT11 permits all-lowercase or all-uppercase Bech32 only. The decoder normalizes case
    # before parsing, so reject mixed case here instead of silently accepting it.
    if invoice.lower() != invoice and invoice.upper() != invoice:
        raise InvoiceDecodeError("invoice must not mix uppercase and lowercase characters")
    try:
        decoded = decode(invoice)
    except Exception as exc:
        raise InvoiceDecodeError("invoice is malformed or has an invalid signature") from exc

    if decoded.currency != expected_network:
        raise InvoiceDecodeError("invoice network does not match the configured network")
    if decoded.amount_msat is None or int(decoded.amount_msat) <= 0:
        raise InvoiceDecodeError("invoice must contain a positive amount")
    if not decoded.has_payment_hash:
        raise InvoiceDecodeError("invoice has no payment hash")

    current = int(time.time()) if now is None else now
    created_at = int(decoded.date)
    expires_at = int(decoded.expiry_time)
    if created_at > current:
        raise InvoiceDecodeError("invoice is dated in the future")
    if expires_at <= current:
        raise InvoiceDecodeError("invoice has expired")

    payment_hash = decoded.payment_hash
    if len(payment_hash) != 64 or any(c not in "0123456789abcdef" for c in payment_hash):
        raise InvoiceDecodeError("invoice has an invalid payment hash")

    return InvoiceDetails(
        network=decoded.currency,
        amount_msat=int(decoded.amount_msat),
        payment_hash=payment_hash,
        created_at=created_at,
        expires_at=expires_at,
    )
