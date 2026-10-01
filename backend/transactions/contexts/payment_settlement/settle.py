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

## Her four outcomes (B4, L630, FR-7.3)

MATCHED → captured at `PENDING`, then flipped to `SETTLED` by `complete_due` after a short
deferred window (default 30s) · DELAYED → `PENDING` (stays at `IN_PROGRESS`, operator-retried)
· UNMATCHED → settles like MATCHED (an alias for a fee-deducted statement line) · EXCEPTION → `RETURNED`. Every value comes from the spec's
`settlementStatus` enum — no invented states. The `outcome` classification itself is an enum on
`settlementPositions` (`MATCHED`/`UNMATCHED`/`DELAYED`/`EXCEPTION`), matched to the spec's
`settlementStatus` enum via `_OUTCOME_TO_STATUS`. Doina (Sep 17): the four outcomes must be
reachable and trigger distinct downstream behaviour, not just label the same result — UNMATCHED
settles, the correspondent's camt.053 books it $25 short (FEE_DEDUCTED, set at initiation), and
stage 8 raises the RECONCILIATION_DISCREPANCY (Sep 17 L1264-1270: GL posts $25,000, statement
shows $24,975). Reconciliation plan Decision 1: the statement is the only source of the $25.

## Why MATCHED is deferred (Doina Sep 17)

She flagged payments reading `SETTLED` "before settlement is even initiated." The settlement is
simulated, so it *can* complete instantly — but applying `SETTLED` synchronously in the same
request that initiated the wire erased the clearing-and-settlement stage from the screen. `run`
now captures at `PENDING` and `complete_due` (driven by `settlement_completion_worker`) flips to
`SETTLED` after `SETTLEMENT_COMPLETION_DELAY_SECONDS`. The ledger's `settlement_worker` then
produces the settlement `ledgerEvent` from the flip, unchanged.

Reads  ctx: current_state, payment_doc, payment_id, payment_rail, is_external_creditor,
            execution_strategy, collections
Writes ctx: current_state -> FAILED | RETURNED (or stays IN_PROGRESS for matched/delayed);
            result; `payments.lifecycle.settlementStatus`, `payments.clearing.*`;
            `settlementPositions` (one doc per run). The MATCHED `SETTLED` flip is NOT here —
            `complete_due` writes it after the deferred window.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
import os
from typing import Optional

from bson import ObjectId

from contexts.payment_order_initiation.domain import checks, lifecycle
from process.exceptions import (
    CATEGORY_SETTLEMENT_DELAYED,
    CATEGORY_SETTLEMENT_RETURNED,
    SERVICE_TRANSACTIONS,
    record_exception,
)
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

# Reconciliation plan A2/A3 — how long after settling a correspondent statement line is due.
# Default two statement cycles (2 × STATEMENT_INTERVAL_SECONDS' 300s default); A3's watchdog
# raises RECONCILIATION_MISSING past `expectedWindow.by`.
_STATEMENT_EXPECTED_WITHIN_SECONDS = int(os.getenv("STATEMENT_EXPECTED_WITHIN_SECONDS", "600"))


def _stamp_expected_window(db, payment_id: str, settled_at: datetime) -> None:
    """Record on the settlement position when its statement line is due (DR-7.1 drift).
    Written at the SETTLED flip, the moment the correspondent can book the wire."""
    db["settlementPositions"].update_one(
        {"paymentId": payment_id, "settlementStatus": "SETTLED"},
        {"$set": {"expectedWindow": {
            "from": settled_at,
            "by": settled_at + timedelta(seconds=_STATEMENT_EXPECTED_WITHIN_SECONDS),
        }}},
    )


# --- her four outcomes (B4, L630, FR-7.3) → spec settlementStatus enum -------
# delayed is PENDING with a future settlementDate — a timing property, not a state
# (doc 21 B4: "Do not invent states").
#
# Uppercase to match the repo's enum convention (settlementStatus is uppercase) and the
# `settlementPositions.outcome` enum declared in the consolidated spec. The request
# contract (`api_models.SettlementOutcomeLiteral`) admits only these four — an unknown
# value 422's at the boundary, never reaches `_OUTCOME_TO_STATUS` (which previously raised
# KeyError → HTTP 500 on a free string).
MATCHED = "MATCHED"
DELAYED = "DELAYED"
UNMATCHED = "UNMATCHED"
EXCEPTION = "EXCEPTION"

OUTCOME_VALUES = (MATCHED, DELAYED, UNMATCHED, EXCEPTION)

_OUTCOME_TO_STATUS: dict[str, str] = {
    MATCHED: "SETTLED",
    DELAYED: "PENDING",
    # Sep 17 L1264-1270: an unmatched settlement still SETTLES — the GL posts the full
    # amount and stage 8 catches the short-pay (statement line ≠ GL settlement posting).
    UNMATCHED: "SETTLED",
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

    if outcome in (MATCHED, UNMATCHED):
        # UNMATCHED settles like MATCHED; its $25 arrives on the correspondent's camt.053
        # (FEE_DEDUCTED alias, reconciliation plan Decision 1), never on this response.
        resp["settlementDate"] = now.date().isoformat()
        resp["statusCode"] = "ACCC"  # ISO 20022: Accepted - Settlement Completed
    elif outcome == DELAYED:
        resp["settlementDate"] = (now + timedelta(days=1)).date().isoformat()
        resp["statusCode"] = "ACSP"  # Accepted - Settlement In Progress
    elif outcome == EXCEPTION:
        resp["statusCode"] = "RJCT"
        resp["returnCode"] = "RETURNED_EXCEPTION"

    return resp


def _write_settlement_position(ctx: PaymentContext, response: dict, model: str) -> str:
    """R12 / FR-7.4 / FR-7.6 — one `settlementPositions` document per settlement run.

    Records the **expected** position (per the payment's clearing instruction = the amount
    credited to the clearing account at stage 6, read from the `transactions` doc) and the
    **actual** position (per the simulated response) as separate stored values, so stage-8's
    three-way reconciliation has a stored pair to compare (FR-7.4). For a cross-border FX
    wire, the correspondent/nostro FX exchange is recorded here too (FR-7.6): `fxRate`, the
    instructed amount/currency, and the settlement amount/currency. The FX exchange is
    metadata only — the settlement ledgerEvent posts the clearing amount (single-currency)
    so 1131 nets to zero; no FX conversion leg.

    Written for EVERY outcome (matched/delayed/unmatched/exception), not just matched, so a
    non-matched settlement leaves a stored record. `actualAmount` is None for matched/unmatched
    (the correspondent's statement line supplies it — reconciliation plan A2), 0 for exception
    (nothing settled), None for delayed (still pending).

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
    settlement_amount = payment.get("amount", 0)
    settlement_currency = payment.get("currency", "USD")

    # Expected = the clearing amount (what was credited to 1131 at stage 6), read from the
    # `transactions` doc — the same source `settlement_worker` uses for the event legs, so
    # leg 2 (expected vs posted debit) compares like-for-like and 1131 nets to zero.
    txn = c.db["transactions"].find_one({"paymentId": ctx.payment_id}) or {}
    expected_amount = txn.get("amount", 0)
    expected_currency = txn.get("currency", "USD")

    outcome = response["outcome"]
    if outcome in (MATCHED, UNMATCHED):
        # Reconciliation plan A2: the actual amount comes only from the correspondent's
        # statement line (the ledger's `statement_matching`). Until then leg 2 waits.
        actual_amount = None
        actual_currency = None
    elif outcome == EXCEPTION:
        actual_amount = 0
        actual_currency = expected_currency
    else:  # DELAYED — settlement not yet confirmed
        actual_amount = None
        actual_currency = None

    doc = {
        "_id": oid,
        "settlementPositionId": position_id,
        "paymentId": ctx.payment_id,
        "rail": ctx.payment_rail,
        "model": model,
        "modelLabel": model_info["label"],
        "clearingAccountCode": _WIRE_CLEARING_CODE,
        "settlementAccountCode": model_info["settlementAccountCode"],
        # FR-7.4: expected vs actual as separate stored values.
        "expectedAmount": expected_amount,
        "expectedCurrency": expected_currency,
        "actualAmount": actual_amount,
        "actualCurrency": actual_currency,
        # Settlement-currency gross (the nostro view); equals expectedAmount when no FX.
        "grossAmount": settlement_amount,
        "currency": settlement_currency,
        "outcome": outcome,
        "settlementStatus": _OUTCOME_TO_STATUS[outcome],
        "batchRef": response["batchRef"],
        "statusCode": response.get("statusCode"),
        "rejectionCode": response.get("rejectionCode"),
        "returnCode": response.get("returnCode"),
        "simulated": True,
        "createdAt": datetime.now(timezone.utc),
        "sourceSystem": "leafy-bank-payments-service",
    }

    # FR-7.6: record the correspondent/nostro FX exchange for a cross-border wire.
    if payment.get("fxRate") is not None:
        doc["fxRate"] = payment.get("fxRate")
        doc["instructedAmount"] = payment.get("instructedAmount")
        doc["instructedCurrency"] = payment.get("instructedCurrency")
        doc["settlementAmount"] = settlement_amount
        doc["settlementCurrency"] = settlement_currency

    c.db["settlementPositions"].insert_one(doc)
    logger.info(
        "settlementPositions %s created for paymentId=%s (model=%s, outcome=%s)",
        position_id, ctx.payment_id, model, outcome,
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


def run(ctx: PaymentContext, defer: bool = True) -> None:
    """Settle a payment.

    No-op for internal transfers (already SETTLED in stage 5's ACID block). For external
    wires, generates a SIMULATED settlement response and transitions the payment per B4.

    `defer` (default True) applies only to the MATCHED outcome: the saga passes True so a
    default wire is captured at `settlementStatus=PENDING` and flipped to SETTLED later by
    `complete_due` (the visible clearing-and-settlement window). The operator
    `settle_payment` route passes False — a manual retry should settle immediately, not wait
    another deferred window. DELAYED/UNMATCHED/EXCEPTION are unaffected by `defer`.
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

    # --- R12: write the settlementPositions document (every outcome) -------------
    # FR-7.4: record expected vs actual for ALL four outcomes, not just matched, so a
    # non-matched settlement leaves a stored record for stage-8 reconciliation
    # (otherwise legs 2/3 sit PENDING forever with no signal). FR-7.6 records the FX
    # exchange on the doc when stage 3 levied an FX rate.
    _write_settlement_position(ctx, response, model)

    # --- B4: transition the payment per the outcome ------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    # `extra` carries the settlementStatus + clearing fields for the non-deferred outcomes
    # (DELAYED / UNMATCHED / EXCEPTION). MATCHED defers and does its own capture write above.
    extra: dict = {
        "lifecycle.settlementStatus": _OUTCOME_TO_STATUS[outcome],
        "clearing.batchRef": response["batchRef"],
    }

    if outcome in (MATCHED, UNMATCHED):
        if defer:
            # Deferred settlement (Doina Sep 17: "wires should settle only when they have
            # reached and completed the clearing & settlement stage"). The settlement is
            # simulated, but the SETTLED flip is NOT applied synchronously here — the payment
            # stays at IN_PROGRESS with settlementStatus PENDING, and
            # `settlement_completion_worker` flips it to SETTLED after
            # SETTLEMENT_COMPLETION_DELAY_SECONDS (default 30s). That gives a visible
            # "settlement pending -> settled" window on the list instead of an instant green
            # SETTLED pill — the exact gap she flagged ("SETTLED before settlement is even
            # initiated").
            #
            # The routing fields the ledger's `settlement_worker` needs (settlementAccountCode,
            # batchRef, settlementDate) are stamped NOW, at capture, so they are already on the
            # doc when the completion worker flips SETTLED and the CDC worker reads them.
            # `clearing.settledAt` is stamped at COMPLETION (`complete_due`), when settlement
            # actually confirms — never here. The `settlementPositions` doc records the expected
            # settlement at capture.
            ctx.collections.payments.update_one(
                {"_id": ctx.payment_oid},
                {"$set": {
                    "lifecycle.settlementStatus": "PENDING",
                    "clearing.batchRef": response["batchRef"],
                    "clearing.settlementDate": response.get("settlementDate"),
                    "clearing.settlementAccountCode": model_info["settlementAccountCode"],
                    "updatedAt": datetime.now(timezone.utc),
                }},
            )
            _record_check(
                ctx, "settlement_initiated", checks.PASS,
                f"Settlement initiated — {model_info['label']} "
                f"({model_info['settlementAccountCode']}). Awaiting simulated confirmation; "
                f"payment held at IN_PROGRESS with settlementStatus PENDING.",
            )
            updated = ctx.collections.payments.find_one({"_id": ctx.payment_oid})
            ctx.stop(updated)
            return
        # `defer=False` — operator-triggered (settle_payment route): settle synchronously,
        # as before the deferral. A manual retry should not wait another deferred window.
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
        _stamp_expected_window(ctx.collections.db, ctx.payment_id, datetime.fromisoformat(now_iso))
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
        # Stage 9 — queue the delay so the Operations lens + RETRY_SETTLEMENT action can
        # reach it (doc 24 B3 site 3). The payment stays IN_PROGRESS; the exception is OPEN.
        record_exception(
            ctx.collections, ctx.payment_doc or {},
            CATEGORY_SETTLEMENT_DELAYED,
            detail={
                "expectedAmount": (ctx.payment_doc or {}).get("amount", 0),
                "actualAmount": None,  # still pending
                "discrepancyAmount": None,
                "discrepancyReason": None,
                "returnCode": None,
                "duplicateOf": None,
            },
            source={"stage": STAGE, "service": SERVICE_TRANSACTIONS},
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
        # Stage 9 — queue the returned settlement (doc 24 B3 site 2). RETURN_FUNDS resolves
        # it via the compensation path (step 6); the clearing position reversal lives there.
        record_exception(
            ctx.collections, ctx.payment_doc or {},
            CATEGORY_SETTLEMENT_RETURNED,
            detail={
                "returnCode": response.get("returnCode"),
                "expectedAmount": (ctx.payment_doc or {}).get("amount", 0),
                "actualAmount": 0,  # nothing settled back
                "discrepancyAmount": None,
                "discrepancyReason": None,
                "duplicateOf": None,
            },
            source={"stage": STAGE, "service": SERVICE_TRANSACTIONS},
        )
        ctx.stop(ctx.payment_doc)
        return


def complete_due(connection, db_name: str, delay_seconds: float = 30.0) -> int:
    """Deferred-settlement completion — the second half of Stage 7 for a MATCHED wire.

    `run` captures a default wire at `settlementStatus=PENDING` (instead of settling
    synchronously). This function flips captured wires to `SETTLED` once their
    `clearing.submittedAt` is older than `delay_seconds`, giving the visible "settlement
    pending -> settled" progression on the list that Doina asked for (Sep 17: "wires should
    settle only when they have reached and completed the clearing & settlement stage").

    Selection — all four must hold:
      * `lifecycle.settlementStatus == PENDING`
      * `lifecycle.currentState == IN_PROGRESS`
      * `clearing.batchRef` present and non-null  (excludes the VOSTRO stub, which sets it null)
      * `simulatedSettlementOutcome in {None, MATCHED, UNMATCHED}`  (excludes DELAYED, operator-retried)
      * `clearing.submittedAt <= now - delay_seconds`
    EXCEPTION never reaches here — `run` marks it RETURNED (terminal), not PENDING. UNMATCHED
    completes like MATCHED; its short-pay arrives on the correspondent's statement.

    Idempotent: completion flips `settlementStatus` to SETTLED, so a later cycle's query cannot
    reselect the same payment. A race between two cycles is closed by `lifecycle.advance`'s
    `from_state=IN_PROGRESS` guard — the loser's `find_one_and_update` matches nothing and raises
    `IllegalTransition`, caught and treated as already-done. After the flip, the ledger's
    `settlement_worker` (CDC on `settlementStatus=SETTLED`) produces the settlement `ledgerEvent`
    — exactly as it did when `run` settled synchronously.

    Returns the number of payments completed this cycle. Always returns a count (never raises
    on an empty scan), so the periodic worker that calls this cannot busy-loop on "no work".
    """
    payments = connection.get_database(db_name)["payments"]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=delay_seconds)
    query = {
        "lifecycle.settlementStatus": "PENDING",
        "lifecycle.currentState": "IN_PROGRESS",
        # `$ne: None` rather than `$exists` so this also excludes VOSTRO (batchRef is null on
        # the stub) and any payment that never reached settlement capture. Missing -> None
        # -> excluded, which is the intent.
        "clearing.batchRef": {"$ne": None},
        "simulatedSettlementOutcome": {"$in": [None, MATCHED, UNMATCHED]},
        "clearing.submittedAt": {"$lte": cutoff},
    }
    completed = 0
    for doc in payments.find(query, {"_id": 1, "paymentId": 1}):
        payment_oid = doc["_id"]
        payment_id = doc.get("paymentId")
        now = datetime.now(timezone.utc)
        try:
            lifecycle.advance(
                payments, payment_oid, lifecycle.SETTLED,
                actor="payment-settlement-worker",
                reason="SIMULATED settlement confirmed (deferred completion)",
                from_state=lifecycle.IN_PROGRESS,
                extra={
                    "lifecycle.settlementStatus": "SETTLED",
                    "clearing.settledAt": now.isoformat(),
                },
            )
        except lifecycle.IllegalTransition:
            logger.info(
                "complete_due: payment %s no longer at IN_PROGRESS (race); skipping", payment_id,
            )
            continue
        _stamp_expected_window(connection.get_database(db_name), payment_id, now)
        checks.append_checks(
            payments, payment_oid,
            [checks.check(
                STAGE, "settlement_completed", checks.PASS,
                mode=checks.SYNC,
                detail=(
                    f"Payment SETTLED (deferred). Clearing account {_WIRE_CLEARING_CODE} debited, "
                    "settlement account credited by the ledger service via CDC."
                ),
                actor="payment-settlement-worker", at=now,
            )],
        )
        completed += 1
        logger.info("complete_due: settled payment %s after deferred window", payment_id)
    return completed
