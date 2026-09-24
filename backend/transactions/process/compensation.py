"""Stage 9 — exceptions, repairs, and returns.

**No BIAN Service Domain**, verified against the v14 landscape: no rows for Exception,
Repair, or Return. That is the answer, not a gap in the search — and it confirms doc 09 §4.
Compensation is a *saga* concern, not a domain one, which is why this file sits in
`process/` beside `payment_lifecycle.py` rather than under `contexts/`.

Put it in a context and every context grows its own retry mechanism.

Doina's rule is the whole design: *"repairs, recalls, returns, and retries should create
additional execution artifacts rather than overwrite the original."* Get `paymentExecutions`
append-only (plan §8 item 4) and most of this stage falls out for free — a return is a new
execution artifact, not a mutation of the old one.

## return_of_funds (doc 24 B5)

The return-of-funds path for a FAILED/RETURNED external wire — the stage's meat. A
RETURNED/UNMATCHED wire leaves the debtor debited and the clearing account `1131` holding;
this restores both in one ACID transaction (the stage-5 `_money_move` pattern inverted):

  1. `$inc` the debtor account **back** up by the held amount; `$inc` `1131` down.
  2. Insert a **compensating `transactions` doc** (`reversalOf` set) — the ledger's
     `ingest_worker` detects `reversalOf` and posts swapped legs (Dr 1131 / Cr customer
     deposit), so 1131 nets back to zero via the same CDC pipeline as the original.
  3. Append a `checks[]` evidence entry on the payment.

Never a balance rewrite; always a compensating movement. The original transaction doc is
byte-unchanged (R7) — the reversal is a new doc, the link lives on it.

`compensate()` (saga partial-failure) stays stubbed: today the only multi-write step is
stage 5's ACID block, which rolls back on its own, so there is nothing to compensate yet.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from contexts.payment_order_initiation.domain import checks
from contexts.payment_rail import documents
from process.exceptions import STATUS_OPEN, STATUS_RESOLVED
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGE = "9 exceptions"


def compensate(ctx: PaymentContext, failed_stage: str, error: Exception) -> None:
    """Saga partial-failure compensation — NOT wired into `payment_lifecycle.run` yet.

    Today the only multi-write step is stage 5's ACID block, which rolls back on its own, so
    there is nothing to compensate. That changes the moment a stage writes durable artifacts
    before the money move and fails after. Kept as the documented home for that path.
    """
    raise NotImplementedError("stage 9 saga compensation — plan §8, after paymentExecutions")


def return_of_funds(
    collections: Any, payment: dict, exception: dict, *, note: Optional[str] = None,
) -> dict:
    """Reverse a FAILED/RETURNED external wire: restore the debtor, clear `1131`, and write
    a compensating `transactions` doc (``reversalOf`` set) for the ledger to pick up via CDC.

    ``collections`` is the saga's `PaymentCollections` (reached via the resolve endpoint's
    service, like `settle_payment`/`resolve_review`). ``payment`` is the stored payment doc;
    ``exception`` is the `exceptions` occurrence being resolved (its id rides the evidence
    trail for traceability). One ACID transaction, the `_money_move` pattern inverted.

    Idempotent under concurrency (defect B2): the exception's ``OPEN → RESOLVED`` flip is the
    *first* write inside the ACID transaction and is conditional on ``status == OPEN``
    (document-level lock on the exception doc). The first resolver to commit wins the claim
    and moves the money; a racing resolver's conditional matches 0 (status already RESOLVED),
    raises, and the whole transaction aborts before any balance changes. A prior reversal doc
    (``reversalOf`` already present) is a second guard — it catches a replay that somehow
    bypassed the status check. Together they make double-compensation impossible: the money
    moves exactly once for any exception, regardless of double-clicks, concurrent operators,
    or a crash between the money move and a separate status write.

    Returns the compensating `transactions` doc. The payment's own terminal state is
    **unchanged** — FAILED stays FAILED (B4); resolution is evidence alongside the terminal
    state, never a state change.
    """
    c = collections
    payment_id = payment["paymentId"]
    original = c.transactions.find_one({"paymentId": payment_id})
    if original is None:
        raise ValueError(
            f"return_of_funds: no transactions doc for {payment_id} — cannot reverse a "
            "payment with no boundary document."
        )

    amount = original["amount"]
    debtor_id = (original.get("payer") or {}).get("accountId")
    clearing_id = (original.get("payee") or {}).get("accountId")
    if not debtor_id or not clearing_id:
        raise ValueError(f"return_of_funds: original txn {original.get('txnId')} missing payer/payee")

    now = datetime.now(timezone.utc)
    exc_id = exception.get("exceptionId", "unknown")
    original_txn_id = original.get("txnId")

    def callback(session) -> dict:
        # 0. Win the OPEN claim — conditional on status == OPEN, inside the ACID txn. This is
        #    the idempotency guard (B2): a concurrent resolver's conditional matches 0 once
        #    this commit flips the status, so its whole txn aborts before any money moves.
        #    A prior reversal doc (e.g. a replay after a crash between this commit and the
        #    response) is a second guard — the exception may already be RESOLVED but a
        #    duplicate caller could still reach here if the status write raced, so the
        #    reversal-existence check makes the money move itself idempotent.
        claim = c.db["exceptions"].find_one_and_update(
            {"_id": exception["_id"], "status": STATUS_OPEN},
            {"$set": {
                "status": STATUS_RESOLVED,
                "resolution": {
                    "action": "RETURN_FUNDS",
                    "by": "payments-operations",
                    "at": now,
                    "note": note,
                },
                "updatedAt": now,
            }},
            session=session,
        )
        if claim is None:
            raise ValueError(
                f"return_of_funds: exception {exc_id} is no longer OPEN — another resolver "
                "already acted. Aborting before any balance change."
            )
        if c.transactions.find_one({"reversalOf": original_txn_id}, {"_id": 1}, session=session):
            raise ValueError(
                f"return_of_funds: a compensating transaction reversing {original_txn_id} "
                "already exists — aborting to avoid double compensation."
            )

        # 1. Restore the debtor (+amount) and reduce the clearing account (-amount). Same
        #    two `accounts` docs the original `_money_move` touched, inverted.
        debtor_after = c.accounts.find_one_and_update(
            {"accountId": debtor_id},
            {
                "$inc": {
                    "balance.current": amount,
                    "balance.available": amount,
                    "balance.ledger": amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        if debtor_after is None:
            raise ValueError(f"return_of_funds: debtor account {debtor_id} not found")
        clearing_after = c.accounts.find_one_and_update(
            {"accountId": clearing_id},
            {
                "$inc": {
                    "balance.current": -amount,
                    "balance.available": -amount,
                    "balance.ledger": -amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        # The clearing account is the load-bearing counterparty of an external wire
        # (execute.py seeds ACC-CLEARING-WIRE before the money move). A no-match means the
        # debit committed and the credit did not — money destroyed. Abort, never silently
        # succeed (the same invariant _money_move enforces).
        if clearing_after is None:
            raise ValueError(
                f"return_of_funds: clearing account {clearing_id} not found — rolled back."
            )

        # 2. The compensating transactions doc — reversalOf set, same payer/payee/rail/amount.
        rev_doc = documents.compensating_transaction_doc(
            original_txn=original, debtor_after=debtor_after, now=now,
        )
        c.transactions.insert_one(rev_doc, session=session)

        # 3. Evidence on the payment (append-only checks[]). The terminal state is NOT
        #    touched — FAILED stays FAILED (B4).
        entry = checks.check(
            STAGE, "compensation_posted", checks.PASS,
            mode=checks.SYNC,
            detail=(
                f"Return of funds posted for exception {exc_id} — debtor {debtor_id} "
                f"restored by {amount}, clearing {clearing_id} reduced by {amount}. "
                f"Reversal txn {rev_doc['txnId']} (reversalOf {original['txnId']}); "
                "ledger posts Dr 1131 / Cr customer deposit via CDC."
            ),
            actor="exceptions-service",
            at=now,
        )
        checks.append_checks(c.payments, payment["_id"], [entry])
        return rev_doc

    with c.db.client.start_session() as session:
        return session.with_transaction(callback)
