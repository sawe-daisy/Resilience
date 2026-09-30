"""Shared contracts for the disbursement API and its invoice parser.

The API stores no recipient identity and never creates or sends invoices. The payee creates the
invoice; the organisation's wallet pays it; an authenticated organisation counsellor reports the
result. Matching a preimage proves that it hashes to the invoice payment hash, not that the server
independently observed Lightning settlement.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InvoiceDetails:
    network: str
    amount_msat: int | None
    payment_hash: str
    created_at: int
    expires_at: int


class InvoiceDecodeError(ValueError):
    """A BOLT11 invoice is malformed, invalidly signed, or not supported by this deployment."""


# API transition contract: these are the only legal changes. Re-attaching the same invoice,
# resubmitting the same valid proof, and repeating a create with the same key/body are idempotent.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"INVOICE_ATTACHED", "CANCELLED", "EXPIRED"}),
    "INVOICE_ATTACHED": frozenset({"PAYING", "CANCELLED", "EXPIRED"}),
    "PAYING": frozenset({"PAID", "FAILED"}),
    "FAILED": frozenset({"INVOICE_ATTACHED", "CANCELLED"}),
    "PAID": frozenset(),
    "EXPIRED": frozenset(),
    "CANCELLED": frozenset(),
}
