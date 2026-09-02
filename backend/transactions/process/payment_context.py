"""The object threaded through every stage of the payment saga.

One `PaymentContext` per payment attempt. Stages read what earlier stages put here and
add their own results. Nothing else crosses stage boundaries — see `payment_lifecycle.py`
for the invariants.

Field groups, in the order they get populated:

- **request**        set by the caller, never mutated by a stage
- **infrastructure** collections + config, injected by `PaymentsService`
- **resolved**       parties and identifiers, populated by stage 1a (capture)
- **produced**       documents written by later stages
- **control**        `halt` / `result` let a stage end the saga early (idempotent replay)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from bson import ObjectId

from contexts.payment_order_initiation.ports.reference_data import (
    NullReferenceData,
    ReferenceData,
)
from contexts.payment_rail.ports.rail import NullRailGateway, RailGateway


@dataclass
class PaymentCollections:
    """The Mongo handles a stage may touch. Injected so stages stay testable with fakes."""

    db: Any
    customers: Any
    accounts: Any
    payments: Any
    transactions: Any
    notifications: Any
    # Stage 4's two new collections (doc 18 B1). Optional so a context built before this
    # stage existed — or a test that does not reach stage 4 — still constructs.
    payment_orders: Any = None
    routing_snapshots: Any = None
    # Stage 5's two new collections (doc 19 B2, B3). Optional for the same reason as stage
    # 4's: a context built before this stage existed, or a test that never reaches a rail,
    # still constructs. `paymentMessages` is Doina's own rename of the row her target-model
    # table calls `canonicalJsonStorage/paymentMessages (renamed)` (L780) —
    # ⚠️ NOT the live `canonicalJsonStorage`, which is another demo's (doc 19 B2).
    payment_executions: Any = None
    payment_messages: Any = None


@dataclass
class PaymentContext:
    # --- request -------------------------------------------------------------
    customer_ref: str
    debtor_account_ref: str
    instructed_amount: float
    instructed_currency: str
    payment_type: str
    payment_rail: str
    # None for an external beneficiary — the spec makes creditor.accountId nullable and
    # Doina's flagship `wire_domestic` scenario has no Leafy Bank creditor account.
    # `creditor_party` then carries the snapshot straight off the request.
    creditor_account_ref: Optional[str] = None
    creditor_party: Optional[dict] = None
    remittance_unstructured: Optional[str] = None
    remittance_reference: Optional[str] = None
    remittance_invoice_no: Optional[str] = None
    priority: str = "NORMAL"
    charge_bearer: str = "SLEV"
    category_purpose: Optional[str] = None
    requested_execution_date: Optional[date] = None
    channel: str = "API"
    idempotency_key: Optional[str] = None
    # The channel's authentication assertion, as supplied. Optional — stage 2 records
    # `method: NONE` and a SKIP when it is absent, and never a PASS (doc 15 B1).
    authentication: Optional[dict] = None
    # Rail-specific initiation envelopes as supplied by the caller, at most one non-None
    # (enforced by `PaymentOrderInitiateRequest`). See `domain/initiation_envelope.py`.
    wire_details: Optional[dict] = None
    ach_details: Optional[dict] = None
    internal_details: Optional[dict] = None

    # --- infrastructure ------------------------------------------------------
    collections: Optional[PaymentCollections] = None
    payment_limit_usd: float = 0.0
    # Stage 3's reference-data lookups (doc 17 §3 step 1). Defaults to a store that
    # resolves nothing, so a context built without one still runs the whole saga —
    # enrichment records WARN checks and the payment proceeds. That keeps the
    # reference-data store off the money path's list of hard dependencies.
    reference_data: ReferenceData = field(default_factory=NullReferenceData)
    # Stage 5's outbound rail (doc 19 §3 step 3). Defaults to a gateway that refuses every
    # submission and says so, mirroring `NullReferenceData`: a context built without one
    # still runs an internal transfer end to end, and a rail-bound payment records a FAILED
    # attempt rather than raising an AttributeError inside the stage.
    rail_gateway: RailGateway = field(default_factory=NullRailGateway)

    # --- resolved (stage 1a) -------------------------------------------------
    now: Optional[datetime] = None
    payment_oid: Optional[ObjectId] = None
    payment_id: Optional[str] = None
    end_to_end_id: Optional[str] = None
    txn_code: Optional[str] = None
    is_internal: bool = False
    debtor_account: Optional[dict] = None
    # None for an external beneficiary. Every consumer must treat it as optional.
    creditor_account: Optional[dict] = None
    debtor_customer: Optional[dict] = None
    creditor_customer: Optional[dict] = None
    debtor_customer_id: Optional[str] = None
    creditor_customer_id: Optional[str] = None
    is_external_creditor: bool = False

    # --- produced ------------------------------------------------------------
    payment_doc: Optional[dict] = None
    # The lifecycle state as this context last saw it. `lifecycle.advance_ctx` keeps it
    # in sync and uses it as the `from_state` guard, so a lost race fails loudly.
    current_state: Optional[str] = None
    fraud: Optional[dict] = None
    # Stage 4a's output, read by stage 4b. The saga's own mechanism for crossing a stage
    # boundary (see this module's docstring): `paymentOrders` is written at APPROVED, in
    # stage 4b, but the strategy it commits to was decided in stage 4a.
    execution_strategy: Optional[Any] = None
    routing_snapshot_id: Optional[str] = None
    # Stage 5's outputs, so a later stage (or a test) can read what was executed without a
    # second query.
    payment_execution_id: Optional[str] = None
    payment_message_id: Optional[str] = None
    # True when `requestedExecutionDate` is in the future: the payment is warehoused for
    # release and stage 4a halts it at ROUTED (doc 18 B9).
    warehoused: bool = False
    checkpoints: list = field(default_factory=list)

    # --- control -------------------------------------------------------------
    halt: bool = False
    result: Optional[dict] = None

    def stop(self, result: dict) -> None:
        """End the saga here and return `result`. Used for idempotent replay."""
        self.halt = True
        self.result = result
