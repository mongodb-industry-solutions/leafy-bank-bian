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

# Stage 7 (BIAN PaymentSettlement, SD 40033 — no published API). The four simulated
# settlement outcomes (FR-7.3 / Doina Sep 17). Mirrors the `settlementPositions.outcome`
# enum declared in the consolidated spec. Sourced from the spec, not memory.
SettlementOutcomeLiteral = Literal["MATCHED", "UNMATCHED", "DELAYED", "EXCEPTION"]

# Reconciliation plan A1. How the correspondent's camt.053 line for this wire differs from our
# books. A simulation control, authored here (not a spec enum); mirrors
# `financial_gateway.domain.camt053.STATEMENT_OUTCOMES`.
StatementOutcomeLiteral = Literal[
    "CLEAN", "FEE_DEDUCTED", "REFERENCE_ALTERED", "LATE", "AMOUNT_TRANSPOSED",
]

# Stage 2 (BIAN PartyAuthentication, SD 38917). NOT from the `payments` spec — no
# authentication field exists there. This is the channel's assertion about an
# authentication IT performed; the payments hub only verifies and records it
# (doc 15 B1). Widening it is a contract change, so keep the list closed.
AuthenticationMethodLiteral = Literal[
    "PASSWORD", "OTP", "BIOMETRIC", "MTLS", "API_KEY", "NONE"
]

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


class AuthenticationAssertionBody(BaseModel):
    """The channel's assertion that it authenticated the caller (doc 15 B1).

    The payments hub performs no authentication of its own — this is what an upstream
    channel says it already did, and stage 2 records the verified outcome as a
    `PartyAuthenticationAssessment`-shaped block on the payment.

    Optional in its entirety. When absent, stage 2 records `method: NONE` and the
    `customer_authenticated` check as **SKIP**, never PASS: the demo must not claim an
    authentication that did not happen.

    Named with the `Body` suffix and deliberately unlike its field name `authentication`
    — an inner class sharing a field's name collapses to `None`-only under
    `Optional[...]` (umbrella defects.md 2026-04-28, `pydantic-shadow`).
    """

    method: AuthenticationMethodLiteral
    authenticatedAt: Optional[datetime] = None
    sessionRef: Optional[str] = None
    factorCount: int = Field(default=0, ge=0)
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
    # DR-1.1: customer's own internal tracking reference (PO number, contract ID),
    # distinct from endToEndId. Optional; the canonical `payments` spec does not
    # declare it — see `test_payment_document_spec._KNOWN_EXTRAS`.
    clientReference: Optional[str] = None

    # Stage 2. Optional, so every existing caller keeps validating unchanged — with
    # `extra="forbid"` adding an optional field is safe, removing one is not.
    authentication: Optional[AuthenticationAssertionBody] = None

    # Rail-specific envelopes. At most one, and it must match `rail`.
    wireDetails: Optional[WireDetailsBody] = None
    achDetails: Optional[AchDetailsBody] = None
    internalDetails: Optional[InternalDetailsBody] = None

    # Stage 7 demo simulation lever (FR-7.3). Only meaningful for an EXTERNAL wire, whose
    # settlement is deferred to stage 7 (an internal transfer settles atomically in stage 5
    # and ignores this). Defaults to MATCHED, preserving the happy path. The back-office
    # wizard exposes it so all four outcomes are drivable from the screen; an API caller can
    # set it to exercise UNMATCHED/DELAYED/EXCEPTION and their distinct downstream effects.
    # NOT a BIAN initiation field — a simulation control, persisted on the payment doc as
    # `simulatedSettlementOutcome` so it survives a step-up / manual-review hold and resume.
    simulatedSettlementOutcome: Optional[SettlementOutcomeLiteral] = None
    # Reconciliation plan A1 — the correspondent-statement lever. Same contract as the
    # settlement lever above: external wire only, persisted as `simulatedStatementOutcome`
    # so it survives a hold and resume. None = CLEAN.
    simulatedStatementOutcome: Optional[StatementOutcomeLiteral] = None

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

        # An "on-us wire" — WIRE/ACH to a creditor whose account THIS bank holds — is
        # disallowed at the contract (defect 2026-09-08 `discriminator-conflation`). Such a
        # payment takes the wire's rail path but the internal's settlement path: it settles
        # atomically inside stage 5's ACID block and `settle.py` no-ops, so
        # `settlementStatus`/`settlementPositions` are never written and reconciliation legs
        # 2 and 3 sit at PENDING forever with no failure signal. A move to a held account is
        # a book transfer — use rail=INTERNAL. Doina (Sep 17): "wires should settle only when
        # they reached and completed the clearing & settlement stage"; this rule makes that
        # true for every wire the contract accepts.
        if self.rail in ("WIRE", "ACH") and self.creditor.accountId is not None:
            raise ValueError(
                f"{self.rail} to a creditor held at this bank is not supported — "
                "use rail=INTERNAL for a move to a held account."
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


class PaymentOrderResumeRequest(BaseModel):
    """`POST /PaymentOrderProcedure/Resume` — re-enter a payment HELD at the step-up gate.

    The channel collects a second factor (the assertion rides on the `Authorization` bearer
    token, exactly as Initiate), then resumes the SAME payment instead of creating a second
    document — one id end to end (Kiran, 2026-09-09).
    """
    paymentId: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class TransactionAuthorizationResolveRequest(BaseModel):
    """`POST /TransactionAuthorization/Resolve` — an operator's manual-review decision on a
    payment HELD at MANUAL_FRAUD_REVIEW (FR-4.13).

    `decision` is "APPROVED" (commit the authorisation the fraud model withheld; the payment
    continues to settlement) or "REJECTED" (terminate to REJECTED — no money has moved).
    """
    paymentId: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


# Stage 9 — `POST /workflow/exceptions/{exceptionId}/resolve` (doc 24 B4/B6). No BIAN
# service domain (row 9: modeled within the originating domain), so the resolve endpoint rides
# the /workflow ops namespace, sanctioned by the `POST /pipeline/batch/trigger` precedent.
#
# The action enum is the OUTGOING subset of the authored `exceptions` stub's
# `resolution.action` enum — REPAIR/RETURN are reserved for the incoming UTA flow
# (FR-9.IN2) and never legal this stage, so they are absent here and 422 at the boundary.
# `newSettlementOutcome` is only meaningful for RETRY_SETTLEMENT (the operator-chosen
# simulated outcome to re-drive settlement with); it reuses the stage-7 settlement enum.
ResolveActionLiteral = Literal[
    "RETRY_SETTLEMENT", "RETURN_FUNDS", "ACCEPT_DISCREPANCY", "DISMISS",
    # Plan A4. RECHECK / LINK_STATEMENT_ENTRY are accepted here only so the service can
    # answer with the ledger route that runs them (D1a), rather than a bare enum error.
    "POST_ADJUSTMENT", "ESCALATE_TO_CORRESPONDENT", "RECHECK", "LINK_STATEMENT_ENTRY",
]


class ExceptionResolveRequest(BaseModel):
    """`POST /workflow/exceptions/{exceptionId}/resolve` — an operator's resolution of one
    queued exception (doc 24 B4). Resolution is evidence alongside the payment's terminal
    state, never a state change: FAILED stays FAILED even after RETURN_FUNDS restores the
    debtor (B4)."""

    action: ResolveActionLiteral
    note: Optional[str] = None
    # RETRY_SETTLEMENT only — the operator-chosen simulated outcome to re-drive settlement.
    # Ignored for the other actions.
    newSettlementOutcome: Optional[SettlementOutcomeLiteral] = None
    model_config = ConfigDict(extra="forbid")


class InboundMessageRequest(BaseModel):
    """BIAN `POST /FinancialGateway/{id}/Inbound/Initiate` — an arriving external pacs.008.

    A real v14 operation: `FinancialGateway` is SD 30542 with a published semantic API, and
    `Inbound/Initiate` is one of its declared endpoints. So the inbound entry point is BIAN
    alignment rather than an invention (D-IN3).

    ## Why this is a separate contract from `PaymentOrderInitiateRequest`

    An inbound wire is, by definition, a WIRE crediting an account this bank holds — exactly
    the combination `_check_cross_field_rules` rejects for a customer-initiated payment
    (defect 2026-09-08 `discriminator-conflation`). That rule is CORRECT and stays: an
    on-us wire submitted by a customer really is incoherent. What it cannot do is describe
    a message arriving from another bank, which is a different act entirely — no customer,
    no account selection, no rail choice (her L385: the rail is fixed by the channel).

    So the two entry points stay independent. Nothing here relaxes the outbound contract.

    The body is the raw ISO message. It is NOT validated by Pydantic beyond being an object:
    structural validation belongs to `inbound_pacs008.parse`, which runs **after** the raw
    message is persisted (FR-1.IN1) so a malformed message is still stored for inspection.
    Rejecting it at the contract boundary would throw it away — the one outcome her L369
    explicitly requires us to avoid.
    """

    message: dict = Field(
        ...,
        description="The received ISO 20022 pacs.008 document, as sent.",
    )
    model_config = ConfigDict(extra="forbid")


class ReconScenarioRequest(BaseModel):
    """`POST /workflow/demo/recon-scenario` — the walkthrough scenario key. Validated against
    `recon_scenarios.WALKTHROUGH_SCENARIOS` in the route (422), the single source of keys."""

    scenario: str


class UtaResolveRequest(BaseModel):
    """`POST /workflow/exceptions/{exceptionId}/uta` — an operator's UTA resolution.

    FR-9.IN2's two structurally different actions, which is why this is not folded into
    `ExceptionResolveRequest`: REPAIR needs an account id (and nothing else does), and
    RETURN needs an ISO return-reason code. A single model would make both optional on every
    action and validate neither.
    """

    action: Literal["REPAIR", "RETURN"]
    # REPAIR only — the account the operator confirms as the true beneficiary.
    matchedAccountId: Optional[str] = None
    # RETURN only — an ISO 20022 ExternalReturnReason1Code.
    returnReasonCode: Optional[str] = None
    note: Optional[str] = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_action_fields(self):
        if self.action == "REPAIR" and not self.matchedAccountId:
            raise ValueError("REPAIR requires matchedAccountId — the account to credit.")
        if self.action == "RETURN" and not self.returnReasonCode:
            raise ValueError(
                "RETURN requires returnReasonCode — an ISO ExternalReturnReason1Code "
                "the pacs.004 can quote."
            )
        return self


class InboundSimulateRequest(BaseModel):
    """`POST /FinancialGateway/{id}/Inbound/Simulate` — the manual inbound trigger.

    The demo control for the incoming-wire story. `scenario` names the mutation the
    generated message carries (see `simulate.py` for what each one exercises); HAPPY is the
    default because it is the one a presenter clicks mid-story.
    """

    scenario: Literal[
        "HAPPY", "PARTIAL", "MISMATCH", "SANCTIONS", "FX", "DUPLICATE",
    ] = "HAPPY"
    model_config = ConfigDict(extra="forbid")


class StatementGenerateRequest(BaseModel):
    """`POST /FinancialGateway/{id}/Statement/Generate` — the manual statement trigger.

    Books every newly-settled outbound wire on the nostro onto the correspondent's next
    camt.053 (reconciliation plan A1). `includeOrphan` adds one line with no internal
    counterpart — the spec's orphaned-settlement case.
    """

    accountCode: Literal["1111", "1121"] = "1111"
    includeOrphan: bool = True
    model_config = ConfigDict(extra="forbid")


class FraudEvaluationRequest(BaseModel):
    """BIAN `POST /FraudEvaluation/Evaluate`.

    Only the payment reference: every scoring input is read from the stored, fully
    orchestrated payment. Accepting an amount or a party here would let a caller score a
    payment that does not exist — and Doina's R9 is explicit that evaluation runs *"against
    the fully orchestrated payment, not the raw initiation."*
    """
    paymentId: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class PaymentConfirmationRequest(BaseModel):
    """BIAN `POST /PaymentConfirmation/Execute` (D8 — the SD has no published API)."""
    paymentId: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class PaymentSettlementInitiateRequest(BaseModel):
    """BIAN `POST /PaymentSettlement/Initiate` (B6 — SD 40033, no published API).

    Triggers or re-triggers settlement for one payment. The `outcome` field drives
    the simulated settlement response (doc 21 B4): matched (default), delayed,
    unmatched, or exception. When omitted, the saga's own default (matched) applies.
    """
    paymentId: str = Field(min_length=1)
    outcome: Optional[SettlementOutcomeLiteral] = Field(default=None)
    model_config = ConfigDict(extra="forbid")
