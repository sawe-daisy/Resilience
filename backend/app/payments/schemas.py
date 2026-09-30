import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReasonCode = Literal["transport", "pharmacy", "shelter", "food", "other"]


class DisbursementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    amount_sat: int = Field(gt=0, le=2_100_000_000_000_000)
    amount_kes: int = Field(gt=0)
    # Caller-reported FX source, retained for the audit trail. The API does not fetch or certify FX.
    rate_source: str = Field(min_length=1, max_length=120)
    reason_code: ReasonCode


class DisbursementLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_payment_cap_sat: int = Field(gt=0, le=2_100_000_000_000_000)
    daily_cap_sat: int = Field(gt=0, le=2_100_000_000_000_000)

    @model_validator(mode="after")
    def daily_cap_covers_single_payment(self):
        if self.daily_cap_sat < self.per_payment_cap_sat:
            raise ValueError("daily cap must be at least the per-payment cap")
        return self


class InvoiceAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice: str = Field(min_length=20, max_length=4096)


class PaymentProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preimage: str = Field(pattern=r"^[0-9a-f]{64}$")


class DisbursementOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    amount_sat: int
    amount_kes: int
    rate_source: str
    reason_code: str
    state: str
    payment_hash: str | None
    invoice: str | None
    invoice_expires_at: datetime | None
    created_by_pubkey: str
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
