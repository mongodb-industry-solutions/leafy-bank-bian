"""`routingSnapshots` (a collection) and `order{}` (a sub-document on `payments`) — the
two artifacts stage 4 produces. Pure builders, no DB access.

⚠️ **Neither `routingSnapshots` nor a `paymentOrders` collection exists in the canonical
spec.** Counted grep over `Consolidated_..._v34_Aug17.json`: `paymentOrders` 0,
`routingSnapshots` 0, across all 22 collections — yet `payments.refs` names both as FK
targets with explicit FK descriptions (D3). So the relationships were authored and the
collections never were, and these shapes are **ours**, built from Doina's L500 and her
stage table at L928. Doc 18 B1; **Doina ratifies as Q28.** Field names follow her nouns so
the mapping needs no translation table.

## The fold — `paymentOrders` is now `payments.order{}`

`payments` is what the customer **asked for**; the order is what the bank **committed to
execute**. D4 originally chose a separate `paymentOrders` collection over an `order{}`
sub-document because *"the instruction-vs-commitment split is one of the model's strongest
BIAN arguments"*. **Reversed by Doina's Aug 27 target model** (L427-429): she strikes
`paymentOrders` through and asks *"better to add these fields directly in the payments
collection?"* — so the commitment now lives as `payments.order`, written at the APPROVED
transition. The instruction-vs-commitment split is preserved as a sub-document boundary
rather than a collection boundary. `refs.paymentOrderId` (a spec-declared FK, retained)
now points within the same document to `order.paymentOrderId`.

`routingSnapshots` stays a separate collection: Doina leaves it un-struck (L387) and names
its immutability requirement explicitly (L500/L928), so it is not a candidate for folding.

## Timing — from the spec, not from us

Her collections table says the commitment is created *"after validation and
authorization"*; her §3 says *"at orchestration"*. Those read as a conflict (Q5) until you
read the spec's own field description for `refs.paymentOrderId`:

> *"Written at orchestration, after authorization."*

Both, in that order. So:

* `routingSnapshots` — written in `"4 orchestrate"`, at the `ROUTED` transition.
* `payments.order`   — written in `"4 authorize"`, at the `APPROVED` transition.

`test_the_payment_order_does_not_exist_before_the_authorization_decision` asserts the
ordering rather than trusting this docstring.

## `routingSnapshots` is immutable

Doina L500: *"written as an immutable RoutingSnapshot … This protects historical records
from retroactive changes if a BIC, clearing code, or correspondent relationship changes
later."* Enforced two ways: nothing here returns an update, and
`test_nothing_ever_updates_a_routing_snapshot` walks the backend's AST for any
`update_one` / `update_many` / `replace_one` / `find_one_and_update` against the
collection. A comment saying "immutable" is not a guarantee; the AST walk is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId

from contexts.payment_order_initiation.domain import bank_identity
from shared.refs import derive_ref

ROUTING_SNAPSHOTS = "routingSnapshots"

SOURCE_SYSTEM = "leafy-bank-payments-service"


def routing_snapshot(
    *,
    payment: dict,
    strategy,
    resolved_correspondent: Optional[dict],
    now: datetime,
    oid: Optional[ObjectId] = None,
) -> dict:
    """The immutable record of the routing decision, exactly as taken at this moment.

    Her L500 names three things it must capture — *"which clearing network, beneficiary bank
    identifier, and (for cross-border) correspondent/nostro account were selected"*. All
    three are copied **by value**, never referenced: a snapshot that pointed at
    `correspondentBanks` would change when that directory changed, which is the precise
    failure the immutability requirement exists to prevent.

    `resolved_correspondent` is the directory row the caller resolved for the chosen
    correspondent BIC, or `None` on a miss. Recording the resolution (not just the BIC)
    is what makes the snapshot self-contained.
    """
    oid = oid or ObjectId()
    creditor = payment.get("creditor") or {}
    wire = payment.get("wireDetails") or {}
    return {
        "_id": oid,
        "routingSnapshotId": derive_ref("RS", oid),
        "paymentId": payment.get("paymentId"),
        # --- the decision -----------------------------------------------------
        "rail": payment.get("rail"),
        "executionStrategy": strategy.strategy,
        "clearingNetwork": strategy.network,
        "costRank": strategy.cost_rank,
        "valueDate": strategy.value_date,
        "cutoffHourET": strategy.cutoff_hour_et,
        "withinCutoff": strategy.within_cutoff,
        "rationale": strategy.rationale,
        # --- the beneficiary bank identifier, by value ------------------------
        "beneficiaryAgent": {
            "bic": creditor.get("bic"),
            "bankName": creditor.get("bankName"),
            "country": creditor.get("bankCountry"),
            "clearingSystemCode": creditor.get("clearingSystemCode"),
            "clearingSystemMemberId": creditor.get("clearingSystemMemberId"),
        },
        # --- our own side, by value (stage 3's B6 single definition) ----------
        "instructingAgent": {
            "bic": bank_identity.OUR_BIC,
            "bankName": bank_identity.OUR_BANK_NAME,
            "country": bank_identity.OUR_BANK_COUNTRY,
            "clearingSystemCode": bank_identity.OUR_CLEARING_SYSTEM_CODE,
            "clearingSystemMemberId": bank_identity.OUR_ABA,
        },
        # --- correspondent / nostro (cross-border only) -----------------------
        # ⚠️ SIMULATED, and the field says so. No correspondent-relationship or nostro
        # account data exists anywhere in the canonical model (doc 18 B5), so `nostro` is
        # deliberately null rather than invented — it needs the chart-of-accounts
        # extension that is Doina/Payton's call (doc 08 "Explicitly not decided", Q9).
        "correspondent": {
            "required": strategy.requires_correspondent,
            "bic": strategy.correspondent_bic,
            "resolved": bool(resolved_correspondent),
            "bankName": (resolved_correspondent or {}).get("bankName"),
            "country": (resolved_correspondent or {}).get("country"),
            "nostroAccountRef": None,
            "simulated": True,
        },
        "messageStandard": wire.get("messageDefinitionIdentifier"),
        "wireType": wire.get("wireType"),
        # --- provenance -------------------------------------------------------
        "decidedAt": now,
        "decidedBy": "orchestration-service",
        "immutable": True,
        "sourceSystem": SOURCE_SYSTEM,
        "createdAt": now,
    }


def payment_order(
    *,
    payment: dict,
    strategy,
    routing_snapshot_id: Optional[str],
    authorization: dict,
    now: datetime,
    warehoused: bool = False,
    oid: Optional[ObjectId] = None,
) -> dict:
    """The bank's execution commitment, written after the authorization decision as a
    sub-document on `payments` (`payments.order`).

    Doina's Aug 27 target model (L427-429) strikes the `paymentOrders` collection through
    and asks to add those fields directly in `payments`; this builder produces the folded
    sub-document. Doc 12 §3 listed the contents: *"selected rail, clearing network, value
    date, confirmed amount/currency, FX, charges, execution conditions, routing decision"*.
    All of them are here. `paymentId` / `customerId` are intentionally NOT repeated — they
    already live on the owning `payments` document — and there is no `_id`: the sub-document
    is addressed by its parent. `paymentOrderId` is kept as the commitment's own reference
    (the bank's execution-commitment id, distinct from `paymentId`), and `refs.paymentOrderId`
    points to it within the same document.

    ⚠️ **`confirmedAmount` must equal `payments.amount`.** A fee is *recorded*, never
    deducted (doc 17 §7): `amount` is the ledger's primary input across the boundary, and a
    payment order that quietly nets the fee out would be a boundary change disguised as a
    fee. `test_the_payment_order_confirms_the_instructed_amount_unchanged` holds it.
    """
    oid = oid or ObjectId()
    return {
        "paymentOrderId": derive_ref("PO", oid),
        # --- the commitment ---------------------------------------------------
        "rail": payment.get("rail"),
        "type": payment.get("type"),
        "clearingNetwork": strategy.network,
        "executionStrategy": strategy.strategy,
        "valueDate": strategy.value_date,
        "confirmedAmount": payment.get("amount"),
        "confirmedCurrency": payment.get("currency"),
        "fxRate": payment.get("fxRate"),
        "charges": list(payment.get("fees") or []),
        "chargeBearer": payment.get("chargeBearer"),
        # --- execution conditions --------------------------------------------
        # `releaseAt` is Payment Warehousing (her L536): a future-dated payment is
        # committed now and released later. Doc 18 B9 builds the hold, not the scheduler.
        "executionConditions": {
            "requestedExecutionDate": payment.get("requestedExecutionDate"),
            "releaseAt": payment.get("requestedExecutionDate"),
            "warehoused": warehoused,
            "withinCutoff": strategy.within_cutoff,
            "priority": payment.get("priority"),
        },
        # --- the decisions this order rests on --------------------------------
        "routingSnapshotId": routing_snapshot_id,
        "authorization": dict(authorization or {}),
        "status": "COMMITTED",
        # --- provenance -------------------------------------------------------
        "committedAt": now,
        "committedBy": "orchestration-service",
        "sourceSystem": SOURCE_SYSTEM,
    }
