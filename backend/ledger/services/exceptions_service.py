"""Stage 9 — the ledger's `exceptions` writer (doc 24 §3 step 3, site 4).

Mirror of the transactions service's `process/exceptions.py`. The two services write ONE
collection with ONE shape; there is no shared import path across services, so this is a
deliberate twin — the stub `backend/ledger/data/exceptions_schema.json` is byte-identical
to `backend/transactions/data/exceptions_schema.json` (asserted on the transactions side),
and the constants below are the stub's code-side twin, parity-asserted on the ledger side.
Same mirror-drift discipline as `bian-alias-map.json` (defects.md).

Only site 4 (`reconciliation_service._stamp_discrepant`) writes from the ledger this
stage, and only the `RECONCILIATION_DISCREPANCY` category. The full category/constant set
is defined anyway so the parity test can assert the whole enum matches the stub — a
partial twin would let the unstated values drift silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from shared.refs import derive_ref

logger = logging.getLogger(__name__)

# --- enum constants (twin of exceptions_schema.json — parity-tested) ----------

CATEGORY_SETTLEMENT_UNMATCHED = "SETTLEMENT_UNMATCHED"
CATEGORY_SETTLEMENT_RETURNED = "SETTLEMENT_RETURNED"
CATEGORY_SETTLEMENT_DELAYED = "SETTLEMENT_DELAYED"
CATEGORY_RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY"
CATEGORY_DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
CATEGORY_UTA = "UTA"  # reserved for incoming; never written by the outgoing path

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_DISMISSED = "DISMISSED"

SEVERITY_ACTION_REQUIRED = "ACTION_REQUIRED"
SEVERITY_INFORMATIONAL = "INFORMATIONAL"

SERVICE_TRANSACTIONS = "transactions-service"
SERVICE_LEDGER = "ledger-service"

SOURCE_STAGE_VALIDATE = "3 validate"
SOURCE_STAGE_SETTLE = "7 settle"
SOURCE_STAGE_RECONCILE = "8 reconcile"

_SEVERITY_FOR = {
    CATEGORY_SETTLEMENT_UNMATCHED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_SETTLEMENT_RETURNED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_SETTLEMENT_DELAYED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_RECONCILIATION_DISCREPANCY: SEVERITY_ACTION_REQUIRED,
    CATEGORY_DUPLICATE_SIGNAL: SEVERITY_INFORMATIONAL,
}


def record_exception(
    exc_coll: Any,
    payment_id: str,
    category: str,
    detail: Optional[dict],
    source: dict,
) -> dict:
    """Write one `exceptions` occurrence from the ledger, deduping on the open pair.

    Same shape as the transactions service's `record_exception` (B2); the stub is the
    contract. Idempotent: if an OPEN exception already exists for this
    (paymentId, category), it is returned untouched — site 4 re-checks DISCREPANT
    payments every batch pass, so a repeated DISCREPANT must not double-insert (B3).
    """
    if category not in _SEVERITY_FOR:
        # UTA (incoming) or a typo — same guard as the transactions twin.
        raise ValueError(f"record_exception: category {category!r} is not an outgoing category")

    existing = exc_coll.find_one(
        {"paymentId": payment_id, "category": category, "status": STATUS_OPEN}
    )
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    oid = ObjectId()
    doc = {
        "_id": oid,
        "exceptionId": derive_ref("EXC", oid),
        "paymentId": payment_id,
        "category": category,
        "status": STATUS_OPEN,
        "severity": _SEVERITY_FOR.get(category, SEVERITY_ACTION_REQUIRED),
        "source": {"stage": source["stage"], "service": source["service"]},
        "detail": detail,
        "resolution": None,
        "agent": None,
        "createdAt": now,
        "updatedAt": now,
        "sourceSystem": source["service"],
    }
    try:
        exc_coll.insert_one(doc)
    except DuplicateKeyError:
        # Concurrent writer won the (paymentId, category, OPEN) claim — re-read and return.
        # The unique partial index is the authority; check-then-insert alone cannot serialise.
        existing = exc_coll.find_one(
            {"paymentId": payment_id, "category": category, "status": STATUS_OPEN}
        )
        if existing is not None:
            return existing
        raise
    logger.info(
        "exceptions: recorded %s for %s (%s)",
        category, payment_id, doc["exceptionId"],
    )
    return doc
