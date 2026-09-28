"""Payment trace aggregator for the Reconciliation Agent.

Gathers every record sharing a `paymentId` across the lifecycle collections so the agent can
reason over the full trace when a reconciliation mismatch is flagged. Read-only.

The sibling ledger service's `pipeline_read_service.trace_payment` already does most of this
for its UI monitor, but it omits `settlementPositions` and `reconciliationItems` (the two
collections that carry the discrepancy the agent is there to explain). Rather than couple to
that service over HTTP, this builds the trace directly from the shared DB — the agent service
already has `MONGODB_URI` + `LEAFField_DB_NAME` for its reference reads and checkpointer.

## Defensive by design

Collections are shared and field names vary across demos (`paymentId` vs `payment_id`;
`expectedAmount` vs `grossAmount`). Every read is best-effort: a missing collection or an
unfamiliar field shape yields an empty list, not an error — the agent reasons over what is
present. Projections drop `_id` so no `ObjectId` crosses into the agent's context.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ledgerEvents carries one event per "posting leg" of a payment, keyed by idempotencyKey:
#   paymentId            — the principal money move
#   {paymentId}-FEE     — the wire fee
#   {paymentId}-SETTLEMENT — the external settlement leg
#   {paymentId}-REV     — a reversal (stage 9)
_LEDGER_EVENT_KEYS = lambda pid: [pid, f"{pid}-FEE", f"{pid}-SETTLEMENT", f"{pid}-REV"]


def _find(collection, query, projection):
    if collection is None:
        return []
    try:
        return list(collection.find(query, projection))
    except Exception:  # noqa: BLE001 — best-effort; a missing/shared-collection quirk is a miss.
        logger.warning("trace read failed (%s, %s) — skipping", query, exc_info=True)
        return []


def _find_open_recon_exception(db, payment_id):
    """The OPEN RECONCILIATION_DISCREPANCY exception for a payment, or None.

    `gather_trace` is called by the tools with only `payment_id`; the exception carries the
    pre-computed `detail.discrepancyAmount`/`expectedAmount`/`actualAmount` the agent needs,
    so include it even when the caller didn't pass `exception_id` explicitly.
    """
    coll = db["exceptions"] if db is not None else None
    if coll is None:
        return None
    try:
        return coll.find_one(
            {"paymentId": payment_id, "category": "RECONCILIATION_DISCREPANCY",
             "status": "OPEN"},
            {"_id": 0},
        )
    except Exception:  # noqa: BLE001
        logger.warning("open recon exception read failed for %s — skipping", payment_id, exc_info=True)
        return None


def gather_trace(db: Any, payment_id: str, exception_id: str | None = None) -> dict:
    """Assemble the full lifecycle trace for a payment.

    Returns `{payment, executions, transactions, ledgerEvents, settlementPositions,
    reconciliationItems, exception}`. Each key is a list (or dict for the singletons). Empty
    lists / None where a collection is absent or unseeded — the agent reasons over what exists.
    """
    no_id = {"_id": 0}
    payment = db["payments"].find_one({"paymentId": payment_id}, no_id) if db is not None else None

    return {
        "paymentId": payment_id,
        "payment": payment,
        "executions": _find(
            db["paymentExecutions"] if db is not None else None,
            {"paymentId": payment_id}, no_id,
        ),
        "transactions": _find(
            db["transactions"] if db is not None else None,
            {"paymentId": payment_id}, no_id,
        ),
        # ledgerEvents: one per posting leg, keyed by idempotencyKey (principal/fee/settlement/rev).
        "ledgerEvents": _find(
            db["ledgerEvents"] if db is not None else None,
            {"idempotencyKey": {"$in": _LEDGER_EVENT_KEYS(payment_id)}}, no_id,
        ),
        "settlementPositions": _find(
            db["settlementPositions"] if db is not None else None,
            {"paymentId": payment_id}, no_id,
        ),
        "reconciliationItems": _find(
            db["reconciliationItems"] if db is not None else None,
            {"paymentId": payment_id}, no_id,
        ),
        "exception": (
            db["exceptions"].find_one({"exceptionId": exception_id}, no_id)
            if db is not None and exception_id
            else _find_open_recon_exception(db, payment_id)
        ),
    }
