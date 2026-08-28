"""Pydantic request models for the BIAN PaymentOrderInitiation service domain.

Field names use camelCase alias names (matching Mongo storage). The registry
handles BIAN documentation mapping; no wire translation is done at request time.

Inner record types use a `Body` suffix to avoid Pydantic forward-ref shadow bugs
when the parent model declares an Optional field with the same name as the class.

Every `Literal` below is sourced from the canonical spec's `$jsonSchema` enum
arrays (`../../../doinas-research/propose_payments.json`, collection `payments`).
Never hand-roll one — enum drift between this file and the spec has bitten this
project before (umbrella defects.md, 2026-04-28).
"""

from datetime import date, datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Enums, verbatim from the spec's $jsonSchema -----------------------------

PaymentTypeLiteral = Literal[
    "CREDIT_TRANSFER", "DIRECT_DEBIT", "CARD_PAYMENT", "RTP", "STANDING_ORDER"
]
PaymentRailLiteral = Literal["INTERNAL", "WIRE", "ACH", "CARD", "RTP"]
PriorityLiteral = Literal["NORMAL", "HIGH", "URGENT"]
ChargeBearerLiteral = Literal["DEBT", "CRED", "SHAR", "SLEV"]
ChannelLiteral = Literal["API", "WEB", "MOBILE", "BRANCH", "BATCH"]
AccountTypeLiteral = Literal["Savings", "Current", "Checking", "FixedDeposit"]
ClearingSystemCodeLiteral = Literal["USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"]
WireTypeLiteral = Literal["DOMESTIC", "INTERNATIONAL"]
SecCodeLiteral = Literal["PPD", "CCD", "WEB", "TEL"]
AchDirectionLiteral = Literal["CREDIT", "DEBIT"]
InternalTransferTypeLiteral = Literal["OWN_ACCOUNT", "THIRD_PARTY"]

# Which rail each initiation envelope belongs to. Used by the cross-field rule
# that rejects an envelope supplied for the wrong rail.
_ENVELOPE_RAIL = {
    "wireDetails": "WIRE",
    "achDetails": "ACH",
    "internalDetails": "INTERNAL",
}


# --- Party records -----------------------------------------------------------

class PaymentDebtorBody(BaseModel):
    accountId: str
    model_config = ConfigDict(extra="forbid")


class PaymentCreditorBody(BaseModel):
    """Mirrors the spec's `creditor` record. `accountId` is nullable — an external
    payee has no Leafy Bank account. The spec requires `accountNo` + `name` on
    every creditor; here they stay optional so an internal caller can keep sending
    `accountId` alone and have the snapshot resolved from the account. The
    cross-field rule below requires them once `accountId` is absent."""

    accountId: Optional[str] = None
    accountNo: Optional[str] = None
    name: Optional[str] = None
    iban: Optional[str] = None
    bic: Optional[str] = None
    bankName: Optional[str] = None
    bankCountry: Optional[str] = Field(default=None, min_length=2, max_length=2)
    address: Optional[str] = None
    accountType: Optional[AccountTypeLiteral] = None
    clearingSystemMemberId: Optional[str] = None
    clearingSystemCode: Optional[ClearingSystemCodeLiteral] = None
    model_config = ConfigDict(extra="forbid")


class PaymentRemittanceBody(BaseModel):
    unstructured: Optional[str] = None
    reference: Optional[str] = None
    invoiceNo: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


# --- Rail-specific initiation envelopes --------------------------------------

class WirePartyBody(BaseModel):
    """pain.001 UltmtDbtr / UltmtCdtr / InitgPty — same two-field shape."""

    name: Optional[str] = None
    identification: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class WireServiceLevelBody(BaseModel):
    code: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class WireLocalInstrumentBody(BaseModel):
    code: Optional[str] = None
    proprietary: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class WirePaymentTypeInformationBody(BaseModel):
    serviceLevel: Optional[WireServiceLevelBody] = None
    localInstrument: Optional[WireLocalInstrumentBody] = None
    model_config = ConfigDict(extra="forbid")


class WireDetailsBody(BaseModel):
    """pain.001 initiation fields a caller may supply. `network`,
    `messageDefinitionIdentifier`, `paymentMethod` and `paymentInformationId` are
    NOT accepted — the first is a routing decision made in stage 4, the rest are
    derived. See `domain/initiation_envelope.py`."""

    wireType: Optional[WireTypeLiteral] = None
    paymentTypeInformation: Optional[WirePaymentTypeInformationBody] = None
    ultimateDebtor: Optional[WirePartyBody] = None
    ultimateCreditor: Optional[WirePartyBody] = None
    initiatingParty: Optional[WirePartyBody] = None
    model_config = ConfigDict(extra="forbid")


class AchDetailsBody(BaseModel):
    """Placeholder — ACH is Phase 2. The effective entry date is NOT here; it
    derives from the common-layer `requestedExecutionDate`."""

    secCode: Optional[SecCodeLiteral] = None
    direction: Optional[AchDirectionLiteral] = None
    model_config = ConfigDict(extra="forbid")


class InternalDetailsBody(BaseModel):
    """`postingReference` is not accepted — it is an FK into `ledgerEvents`,
    written asynchronously by the ledger service."""

    transferType: Optional[InternalTransferTypeLiteral] = None
    model_config = ConfigDict(extra="forbid")


# --- The request -------------------------------------------------------------

class PaymentOrderInitiateRequest(BaseModel):
    customerId: str = Field(min_length=1)
    type: PaymentTypeLiteral
    rail: PaymentRailLiteral
    debtor: PaymentDebtorBody
    creditor: PaymentCreditorBody
    instructedAmount: float = Field(gt=0)
    instructedCurrency: str = Field(min_length=3, max_length=3)
    remittance: Optional[PaymentRemittanceBody] = None

    # Common layer (spec `required`, so defaulted rather than optional).
    priority: PriorityLiteral = "NORMAL"
    chargeBearer: ChargeBearerLiteral = "SLEV"
    categoryPurpose: Optional[str] = None
    requestedExecutionDate: Optional[date] = None
    channel: ChannelLiteral = "API"
    idempotencyKey: Optional[str] = None

    # Rail-specific envelopes. At most one, and it must match `rail`.
    wireDetails: Optional[WireDetailsBody] = None
    achDetails: Optional[AchDetailsBody] = None
    internalDetails: Optional[InternalDetailsBody] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_cross_field_rules(self):
        supplied = [n for n in _ENVELOPE_RAIL if getattr(self, n) is not None]
        if len(supplied) > 1:
            raise ValueError(
                f"At most one initiation envelope may be supplied; got {sorted(supplied)}."
            )
        if supplied and _ENVELOPE_RAIL[supplied[0]] != self.rail:
            raise ValueError(
                f"{supplied[0]} may only be supplied when rail is "
                f"{_ENVELOPE_RAIL[supplied[0]]}; rail is {self.rail}."
            )

        # The spec's validator requires debtor.bic AND creditor.bic to be non-null when
        # rail == WIRE (`validator.$and[1]`). For a creditor we hold, the snapshot resolves
        # the BIC server-side, so demanding it from the caller would reject a perfectly
        # valid internal-account wire. Only an EXTERNAL creditor must carry its own.
        if self.rail == "WIRE" and self.creditor.accountId is None and not self.creditor.bic:
            raise ValueError("creditor.bic is required for an external creditor on the WIRE rail.")

        if self.creditor.accountId is None:
            missing = [f for f in ("accountNo", "name") if not getattr(self.creditor, f)]
            if missing:
                raise ValueError(
                    "An external creditor (no accountId) requires "
                    f"creditor.{' and creditor.'.join(missing)}."
                )

        if self.requestedExecutionDate is None:
            self.requestedExecutionDate = datetime.now(timezone.utc).date()

        if self.instructedCurrency != self.instructedCurrency.upper():
            raise ValueError("instructedCurrency must be an uppercase ISO-4217 code.")

        return self


class PaymentOrderBulkInitiateRequest(BaseModel):
    """A batch of payment orders. Each item is initiated sequentially so the ACID
    balance writes commit in order (no write conflicts on a shared debtor account).
    A per-item failure is reported in the response, not raised — one bad item does
    not abort the batch."""
    items: List[PaymentOrderInitiateRequest] = Field(min_length=1, max_length=50)
    model_config = ConfigDict(extra="forbid")
