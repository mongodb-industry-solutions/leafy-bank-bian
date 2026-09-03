"""Stage 7 — clearing & settlement.

BIAN PaymentSettlement (SD 40033, no published semantic API) + InternalBankAccount
(SD 29306) for the nostro/vostro side.

## Two paths, one stage (doc 21 B3)

* **Internal book transfer** — settlement *is* the money move. `SETTLED` already fired
  inside stage 5's ACID block. This stage is a no-op: the payment arrives at `SETTLED` and
  `run` returns immediately.
* **External wire** — stage 5 left the payment at `IN_PROGRESS`. The money moved (customer
  debited, clearing account credited) but settlement is deferred to here. This stage
  generates a **simulated** settlement response (R8/R9), selects a settlement model (B5),
  transitions the payment per the outcome (B4), and writes a `settlementPositions`
  document (R12).

## The settlement accounting event is NOT here (doc 21 B2, firewall)

`settle.run` writes only to `payments` and `settlementPositions`. The second
`ledgerEvents` document (`Dr 1131 / Cr 1111` or `Cr 1121`) is produced by the **ledger
service's** `settlement_worker` via CDC on `payments` — never by the transactions service
(decisions.md 2026-06-18, the async-CDC firewall).

## Her four outcomes (B4, L630)

matched → `SETTLED` · delayed → `PENDING` (stays at `IN_PROGRESS`) · unmatched → `FAILED`
· exception → `RETURNED`. Every value comes from the spec's `settlementStatus` enum — no
invented states.

Reads  ctx: current_state, payment_doc, payment_id, payment_rail, is_external_creditor,
            execution_strategy, collections
Writes ctx: current_state -> SETTLED | FAILED | RETURNED (or stays IN_PROGRESS for delayed);
            result; `payments.lifecycle.settlementStatus`, `payments.clearing.*`;
            `settlementPositions` (one doc per run)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from contexts.payment_order_initiation.domain import checks, lifecycle
from process.payment_context import PaymentContext
from shared.refs import derive_ref

logger = logging.getLogger(__name__)

STAGE = "7 settle"

# --- settlement models (B5, her L631) ----------------------------------------
# 1. Correspondent  → Cr 1111 Nostro Accounts
# 2. Central bank   → Cr 1121 Minimum Reserve Requirements
# 3. Vostro         → stubbed (Q48 — correspondentBanks has no SSI / settlement-account link)
#
# The GL codes are sourced from the seed, not invented. Both 1111 and 1121 are verified
# ACTIVE L4 posting leaves in the 76-row glAccounts seed (doc 21 §0, step 1 gate).
_SETTLEMENT_MODELS: dict[str, dict] = {
    "CORRESPONDENT": {
        "settlementAccountCode": "1111",
        "settlementAccountName": "Nostro Accounts",
        "label": "Via correspondent bank",
    },
    "CENTRAL_BANK": {
        "settlementAccountCode": "1121",
        "settlementAccountName": "Minimum Reserve Requirements",
        "label": "Direct to central bank",
    },
    "VOSTRO": {
        "settlementAccountCode": None,
        "settlementAccountName": "Vostro (unavailable)",
        "label": "Via vostro counterparty",
    },
}

# The clearing account code for wire ( seeded in step 1).
_WIRE_CLEARING_CODE = "1131"

# --- her four outcomes (B4, L630) → spec settlementStatus enum ---------------
# delayed is PENDING with a future settlementDate — a timing property, not a state
# (doc 21 B4: "Do not invent states").
MATCHED = "matched"
DELAYED = "delayed"
UNMATCHED = "unmatched"
EXCEPTION = "exception"

_OUTCOME_TO_STATUS: dict[str, str] = {
    MATCHED: "SETTLED",
    DELAYED: "PENDING",
    UNMATCHED: "FAILED",
    EXCEPTION: "RETURNED",
}


def _select_model(ctx: PaymentContext) -> str:
    """B5 — pick the settlement model from the routing strategy.

    A payment that required a correspondent (cross-border) settles through the
    correspondent's nostro. A domestic wire settles directly at the central bank. Vostro is
    stubbed — Q48 says `correspondentBanks` models no SSI or settlement-account reference,
    so nothing links a vostro counterparty to the account it would settle through.
    """
    strategy = ctx.execution_strategy
    if strategy is None:
        return "CENTRAL_BANK"
    if getattr(strategy, "requires_correspondent", False) and getattr(strategy, "correspondent_bic", None):
        return "CORRESPONDENT"
    return "CENTRAL_BANK"


def _simulated_response(payment_id: str, model: str, outcome: str) -> dict:
    """R8/R9 — a simulated settlement response, labelled SIMULATED.

    The demo does not connect to a real payment network (her L629, her own bold). The
    response carries the batch reference, the settlement date, and the outcome-specific
    status/rejection/return code — all written to `payments.clearing.*` by the caller.
    """
    now = datetime.now(timezone.utc)
    batch_ref = f"SIM-SETT-{uuid.uuid4().hex[:8].upper()}"

    resp: dict = {
        "batchRef": batch_ref,
        "model": model,
        "outcome": outcome,
        "simulated": True,
        "respondedAt": now.isoformat(),
        "statusCode": None,
        "settlementDate": None,
        "rejectionCode": None,
        "returnCode": None,
    }

    if outcome == MATCHED:
        resp["settlementDate"] = now.date().isoformat()
        resp["statusCode"] = "ACCC"  # ISO 20022: Accepted - Settlement Completed
    elif outcome == DELAYED:
        resp["settlementDate"] = (now + timedelta(days=1)).date().isoformat()
        resp["statusCode"] = "ACSP"  # Accepted - Settlement In Progress
    elif outcome == UNMATCHED:
        resp["statusCode"] = "RJCT"  # Rejected
        resp["rejectionCode"] = "UNMATCHED_AMOUNT"
    elif outcome == EXCEPTION:
        resp["statusCode"] = "RJCT"
        resp["returnCode"] = "RETURNED_EXCEPTION"

    return resp


def _write_settlement_position(ctx: PaymentContext, response: dict, model: str) -> str:
    """R12 — one `settlementPositions` document per settlement run. Returns its ID.

    The collection is absent from the canonical spec (0 matches, doc 21 §0) and from
    Doina's field-level sections — she names it at L744/L926 and never specifies it. This
    shape is authored here and ratify-able via Q51. Access via `ctx.collections.db[...]` so
    neither `PaymentCollections` nor the test `db` fixture needs a new field — the FakeDb's
    `__missing__` yields an empty collection, and the real DB resolves the name directly.
    """
    c = ctx.collections
    oid = ObjectId()
    position_id = derive_ref("SP", oid)

    model_info = _SETTLEMENT_MODELS[model]
    payment = ctx.payment_doc or {}
    amount = payment.get("amount", 0)
    currency = payment.get("currency", "USD")

    doc = {
        "_id": oid,
        "settlementPositionId": position_id,
        "paymentId": ctx.payment_id,
        "rail": ctx.payment_rail,
        "model": model,
        "modelLabel": model_info["label"],
        "clearingAccountCode": _WIRE_CLEARING_CODE,
        "settlementAccountCode": model_info["settlementAccountCode"],
        "grossAmount": amount,
        "currency": currency,
        "outcome": response["outcome"],
        "settlementStatus": _OUTCOME_TO_STATUS[response["outcome"]],
        "batchRef": response["batchRef"],
        "statusCode": response.get("statusCode"),
        "rejectionCode": response.get("rejectionCode"),
        "returnCode": response.get("returnCode"),
        "simulated": True,
        "createdAt": datetime.now(timezone.utc),
        "sourceSystem": "leafy-bank-payments-service",
    }

    c.db["settlementPositions"].insert_one(doc)
    logger.info(
        "settlementPositions %s created for paymentId=%s (model=%s, outcome=%s)",
        position_id, ctx.payment_id, model, response["outcome"],
    )
    return position_id


def _record_check(ctx: PaymentContext, name: str, result: str, detail: str) -> None:
    """Append one check to the payment's checks[] and flush immediately."""
    entry = checks.check(
        STAGE, name, result,
        mode=checks.SYNC, detail=detail,
        actor="payment-settlement-service", at=datetime.now(timezone.utc),
    )
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [entry])


def run(ctx: PaymentContext) -> None:
    """Settle a payment.

    No-op for internal transfers (already SETTLED in stage 5's ACID block). For external
    wires, generates a SIMULATED settlement response and transitions the payment per B4.
    """
    # --- internal book transfer: already settled --------------------------------
    if ctx.current_state == lifecycle.SETTLED:
        _record_check(
            ctx, "settlement_confirmed", checks.SKIP,
            "Internal book transfer — settlement was atomic with the money move (stage 5). "
            "No external settlement needed.",
        )
        return

    # --- only external wires reach IN_PROGRESS without SETTLED (B3) --------------
    if ctx.current_state != lifecycle.IN_PROGRESS:
        logger.info(
            "settle: payment %s is at %s, not IN_PROGRESS — nothing to settle",
            ctx.payment_id, ctx.current_state,
        )
        return

    if not ctx.is_external_creditor:
        logger.warning(
            "settle: payment %s is at IN_PROGRESS but is not external — unexpected, skipping",
            ctx.payment_id,
        )
        return

    # --- B5: select the settlement model -----------------------------------------
    model = _select_model(ctx)
    model_info = _SETTLEMENT_MODELS[model]

    if model == "VOSTRO":
        # B5 — model 3 is stubbed (Q48). The payment stays at IN_PROGRESS with
        # settlementStatus PENDING. A labelled stub shows the model exists but is blocked.
        _record_check(
            ctx, "settlement_model_selected", checks.WARN,
            "Vostro settlement model selected but unavailable (Q48 — correspondentBanks "
            "models no SSI / settlement-account reference). Payment held at IN_PROGRESS.",
        )
        ctx.collections.payments.update_one(
            {"_id": ctx.payment_oid},
            {"$set": {
                "lifecycle.settlementStatus": "PENDING",
                "clearing.batchRef": None,
                "updatedAt": datetime.now(timezone.utc),
            }},
        )
        ctx.stop(ctx.payment_doc)
        return

    _record_check(
        ctx, "settlement_model_selected", checks.PASS,
        f"Model: {model_info['label']} — settlement account {model_info['settlementAccountCode']} "
        f"({model_info['settlementAccountName']}).",
    )

    # --- R9: generate the SIMULATED settlement response --------------------------
    # Default outcome is MATCHED. The BIAN route (step 7) can override this for demo
    # scenarios that need unmatched/delayed/exception outcomes.
    outcome = ctx.settlement_outcome or MATCHED
    response = _simulated_response(ctx.payment_id, model, outcome)

    _record_check(
        ctx, "settlement_response_received", checks.PASS,
        f"SIMULATED response — outcome: {outcome}, status: {response['statusCode']}, "
        f"batch: {response['batchRef']}, value date: {response.get('settlementDate')}.",
    )

    # --- B4: transition the payment per the outcome ------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    extra: dict = {
        "lifecycle.settlementStatus": _OUTCOME_TO_STATUS[outcome],
        "clearing.batchRef": response["batchRef"],
    }

    if outcome == MATCHED:
        extra["clearing.settledAt"] = now_iso
        extra["clearing.settlementDate"] = response.get("settlementDate")
        extra["clearing.settlementAccountCode"] = model_info["settlementAccountCode"]
        lifecycle.advance_ctx(
            ctx, lifecycle.SETTLED,
            actor="payment-settlement-service",
            reason=(
                f"SIMULATED settlement confirmed — {model_info['label']} "
                f"({model_info['settlementAccountCode']})"
            ),
            extra=extra,
        )
        _record_check(
            ctx, "settlement_completed", checks.PASS,
            f"Payment SETTLED via {model_info['label']}. Clearing account {_WIRE_CLEARING_CODE} "
            f"debited, settlement account {model_info['settlementAccountCode']} credited "
            f"(by the ledger service via CDC).",
        )
        # Do NOT ctx.stop — the saga continues to stage 8 (reconciliation, currently a stub).
    elif outcome == DELAYED:
        extra["clearing.settlementDate"] = response.get("settlementDate")
        # settlementStatus is an independent axis (D1) — write it directly, no state transition.
        # IN_PROGRESS -> IN_PROGRESS is not a legal transition, so advance_ctx cannot be used.
        ctx.collections.payments.update_one(
            {"_id": ctx.payment_oid},
            {"$set": {**extra, "updatedAt": now_iso}},
        )
        _record_check(
            ctx, "settlement_delayed", checks.WARN,
            f"Settlement delayed — value date {response.get('settlementDate')} is in the future. "
            f"Payment stays at IN_PROGRESS with settlementStatus PENDING.",
        )
        ctx.stop(ctx.payment_doc)
        return
    elif outcome == UNMATCHED:
        extra["clearing.rejectionCode"] = response.get("rejectionCode")
        lifecycle.advance_ctx(
            ctx, lifecycle.FAILED,
            actor="payment-settlement-service",
            reason=f"SIMULATED settlement rejected — {response.get('rejectionCode')}",
            extra=extra,
        )
        _record_check(
            ctx, "settlement_rejected", checks.FAIL,
            f"Settlement rejected — {response.get('rejectionCode')}. Payment FAILED. "
            f"The clearing position must be reversed (stage 9).",
        )
        ctx.stop(ctx.payment_doc)
        return
    elif outcome == EXCEPTION:
        extra["clearing.returnCode"] = response.get("returnCode")
        lifecycle.advance_ctx(
            ctx, lifecycle.RETURNED,
            actor="payment-settlement-service",
            reason=f"SIMULATED settlement returned — {response.get('returnCode')}",
            extra=extra,
        )
        _record_check(
            ctx, "settlement_returned", checks.FAIL,
            f"Settlement returned — {response.get('returnCode')}. Payment RETURNED. "
            f"The clearing position must be reversed (stage 9).",
        )
        ctx.stop(ctx.payment_doc)
        return

    # --- R12: write the settlementPositions document -----------------------------
    _write_settlement_position(ctx, response, model)
