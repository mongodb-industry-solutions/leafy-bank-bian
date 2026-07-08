"""Pydantic request models for the BIAN PaymentOrderInitiation service domain.

Field names use camelCase alias names (matching Mongo storage). The registry
handles BIAN documentation mapping; no wire translation is done at request time.

Inner record types use a `Body` suffix to avoid Pydantic forward-ref shadow bugs
when the parent model declares an Optional field with the same name as the class.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PaymentDebtorBody(BaseModel):
    accountId: str
    model_config = ConfigDict(extra="forbid")


class PaymentCreditorBody(BaseModel):
    accountId: str
    model_config = ConfigDict(extra="forbid")


class PaymentRemittanceBody(BaseModel):
    unstructured: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class PaymentOrderInitiateRequest(BaseModel):
    customerId: str = Field(min_length=1)
    type: Literal[
        "CREDIT_TRANSFER", "DIRECT_DEBIT", "CARD_PAYMENT", "CHEQUE", "INTRABANK_TRANSFER"
    ]
    rail: Literal["INTERNAL", "ACH", "WIRE", "VENMO", "PAYPAL"]  # drives ledger postingMode routing (BATCH/NEAR_REALTIME/REALTIME)
    debtor: PaymentDebtorBody
    creditor: PaymentCreditorBody
    instructedAmount: float = Field(gt=0)
    instructedCurrency: str = Field(min_length=3, max_length=3)
    remittance: Optional[PaymentRemittanceBody] = None
    model_config = ConfigDict(extra="forbid")


class PaymentOrderBulkInitiateRequest(BaseModel):
    """A batch of payment orders. Each item is initiated sequentially so the ACID
    balance writes commit in order (no write conflicts on a shared debtor account).
    A per-item failure is reported in the response, not raised — one bad item does
    not abort the batch."""
    items: List[PaymentOrderInitiateRequest] = Field(min_length=1, max_length=50)
    model_config = ConfigDict(extra="forbid")


