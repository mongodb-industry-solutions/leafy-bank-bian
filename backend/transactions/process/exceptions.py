"""Stage 9 — the `exceptions` queue writer (doc 24 §3 step 2).

One document per exception **occurrence** (doc 24 B2): a settlement that came back
unmatched/returned/delayed, a reconciliation discrepancy, or a duplicate-content signal.
Occurrence-per-doc — not one-per-payment — matches `paymentExecutions` append-only
(DR-5.1): a payment can fail, be returned, and later draw a duplicate signal, and each is
its own row in the Operations queue.

## Shape

Authored in `backend/transactions/data/exceptions_schema.json` and ratified via Q60. The
ledger service writes the same collection (site 4, `_stamp_discrepant`) with a mirror copy
of the stub at `backend/ledger/data/exceptions_schema.json` — **byte-identical**, asserted
by `test_stage_nine.test_the_ledger_mirror_is_byte_identical_to_the_canonical_stub`. There
is no shared import path across the two services, so the stub is the contract and the
constants below are its code-side twin. The enum-parity test
(`test_the_code_enum_constants_match_the_authored_stub`) asserts the two never drift —
the 2026-04-28 enum-drift prevention rule, discharged against the stub not memory.

## Dedupe

Site 4 re-checks DISCREPANT payments every batch pass (a discrepancy can resolve), so the
writer must not double-insert: keyed lookup on `(paymentId, category, status: OPEN)` —
insert only if no open doc exists, return the existing one otherwise (B3).

## Access path

`collections.db["exceptions"]` — the settle.py pattern for non-spec collections. No new
`PaymentCollections` field; the FakeDb's `__missing__` yields an empty collection, and the
real DB resolves the name directly.
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
# ⚠️ Source these from the stub, never memory (defect 2026-04-28). The parity test
#    asserts every tuple below equals the stub's enum array.

CATEGORY_SETTLEMENT_UNMATCHED = "SETTLEMENT_UNMATCHED"
CATEGORY_SETTLEMENT_RETURNED = "SETTLEMENT_RETURNED"
CATEGORY_SETTLEMENT_DELAYED = "SETTLEMENT_DELAYED"
CATEGORY_RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY"
# Reconciliation plan A3 — written only by the ledger; mirrored here for stub parity.
CATEGORY_RECONCILIATION_MISSING = "RECONCILIATION_MISSING"
CATEGORY_ORPHANED_SETTLEMENT = "ORPHANED_SETTLEMENT"
CATEGORY_DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
# UTA is reserved for the incoming Repair/Return flow (FR-9.IN4) and is never written by
# this outgoing path — it lives in the stub's enum so the incoming build adds no reshape.
CATEGORY_UTA = "UTA"

# Categories this stage actually writes (UTA excluded — reserved for incoming).
OUTGOING_CATEGORIES = (
    CATEGORY_SETTLEMENT_UNMATCHED,
    CATEGORY_SETTLEMENT_RETURNED,
    CATEGORY_SETTLEMENT_DELAYED,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_DUPLICATE_SIGNAL,
)

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_DISMISSED = "DISMISSED"

SEVERITY_ACTION_REQUIRED = "ACTION_REQUIRED"
SEVERITY_INFORMATIONAL = "INFORMATIONAL"

ACTION_RETRY_SETTLEMENT = "RETRY_SETTLEMENT"
ACTION_RETURN_FUNDS = "RETURN_FUNDS"
ACTION_ACCEPT_DISCREPANCY = "ACCEPT_DISCREPANCY"
ACTION_DISMISS = "DISMISS"
# REPAIR / RETURN are reserved for incoming UTA (FR-9.IN2) — never written this stage.
ACTION_REPAIR = "REPAIR"
ACTION_RETURN = "RETURN"
# The ledger auto-resolves RECONCILIATION_MISSING with this once the line arrives (A3 D2).
ACTION_RECHECK = "RECHECK"
# Plan A4 — operator/agent resolution routes. RECHECK and LINK execute on the ledger (D1a).
ACTION_LINK_STATEMENT_ENTRY = "LINK_STATEMENT_ENTRY"
ACTION_POST_ADJUSTMENT = "POST_ADJUSTMENT"
# Not a resolution: recorded in `escalation{}`, the exception stays OPEN (A4 D4).
ACTION_ESCALATE_TO_CORRESPONDENT = "ESCALATE_TO_CORRESPONDENT"

SOURCE_STAGE_VALIDATE = "3 validate"
SOURCE_STAGE_SETTLE = "7 settle"
SOURCE_STAGE_RECONCILE = "8 reconcile"

SERVICE_TRANSACTIONS = "transactions-service"
SERVICE_LEDGER = "ledger-service"

# A payment's terminal state never reopens (B4); resolution is evidence alongside it.
_RESOLVED_STATUSES = frozenset({STATUS_RESOLVED, STATUS_DISMISSED})

# Severity by category — B3's table. DUPLICATE_SIGNAL is the only INFORMATIONAL row.
_SEVERITY_FOR = {
    CATEGORY_SETTLEMENT_UNMATCHED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_SETTLEMENT_RETURNED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_SETTLEMENT_DELAYED: SEVERITY_ACTION_REQUIRED,
    CATEGORY_RECONCILIATION_DISCREPANCY: SEVERITY_ACTION_REQUIRED,
    CATEGORY_RECONCILIATION_MISSING: SEVERITY_ACTION_REQUIRED,
    CATEGORY_ORPHANED_SETTLEMENT: SEVERITY_ACTION_REQUIRED,
    CATEGORY_DUPLICATE_SIGNAL: SEVERITY_INFORMATIONAL,
}


# Typed resolve errors. Subclass ValueError so existing callers/tests keep working; the
# router maps on type, not on message wording.
class ExceptionNotFound(ValueError):
    """404 — the exception or its payment does not exist."""


class ExceptionConflict(ValueError):
    """409 — the resolve conflicts with current state (already closed, raced, wrong state)."""


class ExceptionActionNotLegal(ValueError):
    """422 — the action is not legal for this category."""


def severity_for(category: str) -> str:
    """The severity the queue assigns to a category. Falls back to ACTION_REQUIRED — the
    safe default for an unknown category, since an uncategorised exception is exactly the
    case where a human should look."""
    return _SEVERITY_FOR.get(category, SEVERITY_ACTION_REQUIRED)


def record_exception(
    collections: Any,
    payment: dict,
    category: str,
    detail: Optional[dict],
    source: dict,
) -> dict:
    """Write one `exceptions` occurrence for `payment`, deduping on the open pair.

    `collections` is a `PaymentCollections` (the saga's `ctx.collections`); the exceptions
    collection is reached via `collections.db["exceptions"]` — no `PaymentCollections`
    field is added (doc 24 B2 / settle.py precedent).

    `payment` is the stored payment doc; `paymentId` is read off it. `source` is
    `{"stage": ..., "service": ...}` — the service also becomes the doc's `sourceSystem`.

    Idempotent: if an OPEN exception already exists for this (paymentId, category), it is
    returned untouched and no insert is made. Returns the exception doc (new or existing).
    """
    if category not in OUTGOING_CATEGORIES:
        # UTA (incoming) or a typo — neither is this stage's to write.
        raise ValueError(f"record_exception: category {category!r} is not an outgoing category")

    payment_id = payment.get("paymentId")
    if not payment_id:
        raise ValueError("record_exception: payment carries no paymentId")

    coll = collections.db["exceptions"]

    existing = coll.find_one(
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
        "severity": severity_for(category),
        "source": {"stage": source["stage"], "service": source["service"]},
        "detail": detail,
        "resolution": None,
        "agent": None,
        "createdAt": now,
        "updatedAt": now,
        "sourceSystem": source["service"],
    }
    try:
        coll.insert_one(doc)
    except DuplicateKeyError:
        # The unique partial index on (paymentId, category, status=OPEN) caught a concurrent
        # writer (two reconciliation workers, or a duplicate-content race). The find_one above
        # missed it because the other insert committed between the check and this insert. The
        # index is the authority — re-read the now-existing OPEN row and return it. This is
        # the repo's idempotency pattern (standards.md): unique index + catch, never
        # check-then-insert on its own.
        existing = coll.find_one(
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
