"""Workflow read service — UI-only queries over the `payments` collection.

Read-only, and deliberately separate from `payments_service` (BIAN-facing): these queries
exist to power the back-office Payments Workflow surface and are not part of the
PaymentOrderInitiation contract. Mirrors the ledger's `pipeline_read_service` precedent.

## Why this lives in the transactions service

`payments` is owned here. The ledger's `/pipeline/trace` deliberately never reads it — the
async-CDC boundary (decisions.md 2026-06-18) says `ledgerEvents` derive from `transactions`
alone. The UI composes the two halves client-side: `/workflow/payments/{id}` for stages 1-4,
`/pipeline/trace/{id}` for stages 5-8. Neither service reads the other's collections.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from contexts.payment_order_initiation.domain import lifecycle
from database.connection import MongoDBConnection

# A payment needing attention is one that stopped somewhere it will never leave. The set is
# taken from the state machine rather than restated, so a new terminal state cannot silently
# fall out of the Operations lens.
INTERVENTION_STATES = sorted(lifecycle.TERMINALS)

# The list view never needs the full document — envelopes, correspondent blocks and clearing
# details are only meaningful in the deep dive. Projecting narrowly keeps the payload small
# and, more usefully, keeps the list stable when a later stage adds fields.
_LIST_PROJECTION = {
    "_id": 0,
    "paymentId": 1,
    "createdAt": 1,
    "initiatedAt": 1,
    "customerId": 1,
    "type": 1,
    "rail": 1,
    "status": 1,
    "amount": 1,
    "currency": 1,
    "instructedAmount": 1,
    "instructedCurrency": 1,
    "lifecycle.currentState": 1,
    "lifecycle.stateEnteredAt": 1,
    "debtor.name": 1,
    "debtor.accountId": 1,
    "creditor.name": 1,
    "creditor.accountId": 1,
}


def _payments(connection: MongoDBConnection, db_name: str):
    return connection.get_collection(db_name, "payments")


def _build_filter(
    *,
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    rail: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """Translate the query string into a Mongo filter.

    Filters on `status` rather than `lifecycle.currentState` even though the latter is the
    source of truth: `status` is its mirror (lifecycle.py's contract) and is the top-level
    field an index can serve.
    """
    query: dict = {}
    if status:
        query["status"] = status
    if customer_id:
        query["customerId"] = customer_id
    if rail:
        query["rail"] = rail
    if date_from or date_to:
        window: dict = {}
        if date_from:
            window["$gte"] = date_from
        if date_to:
            window["$lte"] = date_to
        query["createdAt"] = window
    return query


def list_payments(
    connection: MongoDBConnection,
    db_name: str,
    *,
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    rail: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 25,
    skip: int = 0,
) -> dict:
    """Newest-first page of payments, plus the total matching the same filter."""
    coll = _payments(connection, db_name)
    query = _build_filter(
        status=status, customer_id=customer_id, rail=rail,
        date_from=date_from, date_to=date_to,
    )
    cursor = (
        coll.find(query, _LIST_PROJECTION)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    items = list(cursor)
    return {
        "items": items,
        "total": coll.count_documents(query),
        "limit": limit,
        "skip": skip,
    }


def get_payment(connection: MongoDBConnection, db_name: str, payment_id: str) -> Optional[dict]:
    """The whole payment document — `lifecycle.events[]`, `checks[]`, envelopes.

    Returned in full on purpose: this is the deep dive, and every later stage adds fields
    here that its own timeline panel will want. A projection would have to be edited once
    per stage.

    `_id` is excluded rather than stringified — the paymentId is the identifier the UI uses,
    and echoing a raw ObjectId into a response is the 2026-06-11 defect.
    """
    return _payments(connection, db_name).find_one({"paymentId": payment_id}, {"_id": 0})


def list_exceptions(
    connection: MongoDBConnection, db_name: str, *, limit: int = 25, skip: int = 0
) -> dict:
    """Payments that ended in a terminal state — the Operations lens."""
    coll = _payments(connection, db_name)
    query = {"status": {"$in": INTERVENTION_STATES}}
    items = list(
        coll.find(query, _LIST_PROJECTION).sort("createdAt", -1).skip(skip).limit(limit)
    )
    return {
        "items": items,
        "total": coll.count_documents(query),
        "limit": limit,
        "skip": skip,
        "states": INTERVENTION_STATES,
    }


def get_stats(
    connection: MongoDBConnection,
    db_name: str,
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """Throughput, in-flight, exception and value totals for the stats strip.

    Defaults to a trailing 24h window when no dates are given — the demo's natural unit, and
    it keeps the strip meaningful on a cluster holding months of seeded data.
    """
    if date_from is None and date_to is None:
        date_from = datetime.now(timezone.utc) - timedelta(days=1)

    coll = _payments(connection, db_name)
    query = _build_filter(date_from=date_from, date_to=date_to)

    # One pass for the status histogram; the strip's buckets are derived from it rather than
    # issued as separate count_documents calls.
    by_status: dict[str, int] = {}
    for row in coll.aggregate([{"$match": query}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        if row["_id"]:
            by_status[row["_id"]] = int(row["n"])

    totals = list(coll.aggregate([
        {"$match": query},
        {"$group": {"_id": None, "value": {"$sum": "$amount"}, "n": {"$sum": 1}}},
    ]))
    total_count = int(totals[0]["n"]) if totals else 0
    total_value = float(totals[0]["value"]) if totals else 0.0

    settled = by_status.get(lifecycle.SETTLED, 0) + by_status.get(lifecycle.RECONCILED, 0)
    exceptions = sum(by_status.get(s, 0) for s in INTERVENTION_STATES)

    return {
        "window": {
            "from": date_from,
            "to": date_to,
        },
        "total": total_count,
        "totalValue": round(total_value, 2),
        "settled": settled,
        # Anything neither settled nor terminal is still moving through the saga.
        "inFlight": max(total_count - settled - exceptions, 0),
        "exceptions": exceptions,
        "byStatus": by_status,
    }
