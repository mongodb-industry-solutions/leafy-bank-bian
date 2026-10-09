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
from contexts.payment_rail.domain import pacs008
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
    # The incoming wire (2026-09-29). `direction` is what the Activity list marks each row
    # with (inbound vs outbound — the two read very differently: money arriving vs money
    # leaving), and `debtor.bankName` is the external sender's bank, the inbound row's
    # counterparty label. Inclusion-allowlisted like everything else — see the ⚠️ above.
    "direction": 1,
    "debtor.name": 1,
    "debtor.accountId": 1,
    "debtor.bankName": 1,
    "creditor.name": 1,
    "creditor.accountId": 1,
    # Stage 4. The list is the Analyst's lens, and a payment's risk decision is exactly the
    # sort of thing they scan a list for — so `fraud.decision`/`score` and the selected
    # clearing network are projected, while the routing snapshot and payment order stay in
    # the deep dive where they are readable.
    #
    # ⚠️ An INCLUSION allowlist, never an exclusion (defect 2026-08-31 `performance`): these
    # documents live in a database shared across demos, so an exclusion list stops working
    # the moment another demo attaches a fat field. That also means a later stage's field is
    # invisible here until it is named — deliberate, and the reason this comment exists.
    "fraud.decision": 1,
    "fraud.score": 1,
    "wireDetails.network": 1,
    # Stage 6. The posting axis advances independently of `status` (spec: "POSTED is an
    # accounting fact, not a pipeline position"), so a payment can read SETTLED here and
    # still be unposted — which is exactly what a finance operator scans this list for.
    # Written by the LEDGER service (doc 20 B1), not by anything in this service.
    "lifecycle.postingStatus": 1,
    "refs.journalEntryId": 1,
    # Stage 7 / 8. `settlementStatus` and `reconciliationStatus` are the other two
    # independent axes (defect 2026-09-08 `discriminator-conflation`, Doina Sep 17). A
    # payment whose `status` is SETTLED may have `settlementStatus` absent (an internal
    # book transfer — no external settlement) or `postingStatus` still PENDING (the GL
    # batch hasn't run). Surfacing all three lets the list read "settled, posting pending"
    # instead of the misleading single pill Doina flagged.
    "lifecycle.settlementStatus": 1,
    "lifecycle.reconciliationStatus": 1,
}


def _payments(connection: MongoDBConnection, db_name: str):
    return connection.get_collection(db_name, "payments")


# `validation.determinedCategory` is the corridor stage 3 records. Outbound writes the
# lower-case names, inbound writes DOMESTIC / CROSS_BORDER. A payment refused before stage 3
# has no category, so the stage 1 `wireDetails.wireType` is the fallback.
_CORRIDOR_CATEGORIES = {
    "DOMESTIC": ["domestic-same-bank", "domestic-different-bank", "DOMESTIC"],
    "INTERNATIONAL": ["cross-border", "CROSS_BORDER"],
}


def _build_filter(
    *,
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    rail: Optional[str] = None,
    direction: Optional[str] = None,
    corridor: Optional[str] = None,
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
    if direction == "INBOUND":
        query["direction"] = "INBOUND"
    elif direction == "OUTBOUND":
        # `$ne`, not `== "OUTBOUND"`: payments written before the incoming wire carry no
        # `direction` at all, and they are all outbound. `$ne` also matches a missing field.
        query["direction"] = {"$ne": "INBOUND"}
    if corridor in _CORRIDOR_CATEGORIES:
        query["$or"] = [
            {"validation.determinedCategory": {"$in": _CORRIDOR_CATEGORIES[corridor]}},
            {"validation.determinedCategory": {"$exists": False},
             "wireDetails.wireType": corridor},
        ]
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
    direction: Optional[str] = None,
    corridor: Optional[str] = None,
    limit: int = 25,
    skip: int = 0,
) -> dict:
    """Newest-first page of payments, plus the total matching the same filter."""
    coll = _payments(connection, db_name)
    query = _build_filter(
        status=status, customer_id=customer_id, rail=rail, direction=direction,
        corridor=corridor, date_from=date_from, date_to=date_to,
    )
    cursor = (
        coll.find(query, _LIST_PROJECTION)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    items = list(cursor)
    # Join each row with its open (or latest resolved) exception occurrence, so the Activity
    # list surfaces the exception reason + discrepancy subline for failed/returned payments
    # — the same row rendering the Operations queue uses. ONE `$in` for the page. Null for a
    # payment with no exception doc (the common case).
    exc_coll = connection.get_collection(db_name, "exceptions")
    joined = _join_exceptions(exc_coll, [i.get("paymentId") for i in items])
    for item in items:
        item["exception"] = joined.get(item.get("paymentId"))
    return {
        "items": items,
        "total": coll.count_documents(query),
        "limit": limit,
        "skip": skip,
    }


def get_payment(connection: MongoDBConnection, db_name: str, payment_id: str) -> Optional[dict]:
    """The whole payment document — `lifecycle.events[]`, `checks[]`, envelopes — plus
    stage 5's execution artifacts joined on.

    Returned in full on purpose: this is the deep dive, and every later stage adds fields
    here that its own timeline panel will want. A projection would have to be edited once
    per stage.

    **Stage 5 is the first stage whose output is not on the payment document.** Its pacs.008
    lives on `paymentExecutions` and its canonical payload on `paymentMessages`, so the two
    are attached here as `executions[]` and `messages[]`. Doc 16 §5 rule 2 — *extend the
    existing route, do not add a new one per stage* — is why this is a join rather than a
    second endpoint the UI would have to fetch and correlate. Stage 4 got away with exposing
    ref ids only; her BUSINESS VIEW / ISO VIEW pair cannot be rendered from an id.

    `_id` is excluded rather than stringified — the paymentId is the identifier the UI uses,
    and echoing a raw ObjectId into a response is the 2026-06-11 defect.
    """
    payment = _payments(connection, db_name).find_one({"paymentId": payment_id}, {"_id": 0})
    if payment is None:
        return None
    # Empty lists, not absent keys: a payment written before stage 5 existed, or an internal
    # transfer that legitimately has no artifacts (doc 19 B4), must read as "none" rather
    # than making the UI branch on `undefined`.
    payment["executions"] = [
        _with_xml(e)
        for e in connection.get_collection(db_name, "paymentExecutions")
        .find({"paymentId": payment_id}, {"_id": 0})
        .sort("attempt", 1)
    ]
    payment["messages"] = list(
        connection.get_collection(db_name, "paymentMessages")
        .find({"paymentId": payment_id}, {"_id": 0})
        .sort("createdAt", 1)
    )
    # Stage 7's settlementPositions — one doc per settlement run (doc 21 R12, step 6).
    # Empty list for internal transfers (no settlement run) and pre-stage-7 payments.
    payment["settlementPositions"] = list(
        connection.get_collection(db_name, "settlementPositions")
        .find({"paymentId": payment_id}, {"_id": 0})
        .sort("createdAt", 1)
    )
    # Stage 4's routingSnapshots — the immutable execution-strategy decision (doc 18 R7).
    # One per payment (insert-only, written at the ROUTED transition). Attached here so the
    # stage-4 panel can render the strategy, network, correspondent, cut-off and rationale
    # Doina's FR-4.1 asks to be visible — the payment doc carries only the network + the
    # snapshot id; the full decision lives on the snapshot. None for a pre-stage-4 payment.
    snap = connection.get_collection(db_name, "routingSnapshots").find_one(
        {"paymentId": payment_id}, {"_id": 0}
    )
    payment["routingSnapshot"] = snap
    # Stage 9's exceptions — every occurrence for this payment (doc 24 §3 step 8). Empty
    # list for a payment with no exceptions (the common case), so the stage-9 panel reads
    # "no exceptions" rather than branching on undefined. Oldest first; the panel sorts.
    payment["exceptions"] = list(
        connection.get_collection(db_name, "exceptions")
        .find({"paymentId": payment_id}, {"_id": 0})
        .sort("updatedAt", 1)
    )
    # Stage 8's evidence: the camt.053 statement(s) carrying this payment's line. Not in
    # `messages` above because a statement covers many payments (`paymentId` is null).
    # Keyed on `simulatedPaymentId`, not `reference`: an altered reference (R2) still
    # belongs to this payment.
    payment["statements"] = list(
        connection.get_collection(db_name, "paymentMessages")
        .find({"purpose": "ACCOUNT_STATEMENT", "entries.simulatedPaymentId": payment_id},
              {"_id": 0})
        .sort("createdAt", 1)
    )
    return payment


def _with_xml(execution: dict) -> dict:
    """Attach the ISO 20022 XML rendering of the stored message.

    **Derived at read time, never persisted.** The XML is a *rendering* of
    `paymentExecutions.message`, so storing it would keep the same fact in two places and
    invite exactly the drift the mirror-drift entries in defects.md are about — the same
    reasoning that kept the pacs.008 off `payments.wireDetails` (doc 19 B5).

    Honest about what it is not: the rail here is simulated and sends no bytes, so there is no
    "message as transmitted" to preserve. The stored JSON is the artifact; this is a view of
    it. The day a real gateway exists, the bytes it actually sent become the thing to persist.
    """
    message = execution.get("message")
    if message:
        execution["messageXml"] = pacs008.to_xml(message)
    return execution


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _pick_exception(candidates: list) -> Optional[dict]:
    """The one exception a list row renders: the open one, else the most-recently-updated
    resolved/dismissed one, else None. `updatedAt` is tz-aware; fall back to a tz-aware
    epoch so a doc missing both timestamps cannot raise on comparison."""
    if not candidates:
        return None
    open_excs = [e for e in candidates if e.get("status") == "OPEN"]
    if open_excs:
        return open_excs[0]
    return max(
        candidates,
        key=lambda e: e.get("updatedAt") or e.get("createdAt") or _EPOCH,
    )


def _join_exceptions(exc_coll, payment_ids: list) -> dict:
    """Map `paymentId -> the exception to render`, for a whole page of list rows.

    ONE `$in` query for the page, not one per row. The per-row version issued up to `limit`
    (25) separate lookups against a shared, append-only collection on every Activity-list and
    Operations-queue load — and none of them could use `idx_exception_open_unique`, which is
    partial on `status == "OPEN"` and so ineligible for a query that does not constrain
    status. `idx_exception_payment_recent` now serves this one.
    """
    ids = [pid for pid in payment_ids if pid]
    if not ids:
        return {}
    by_payment: dict = {}
    for exc in exc_coll.find({"paymentId": {"$in": ids}}):
        by_payment.setdefault(exc.get("paymentId"), []).append(exc)
    return {pid: _pick_exception(excs) for pid, excs in by_payment.items()}


def _statement_line_row(key: str, exc: Optional[dict]) -> Optional[dict]:
    """A queue row for an exception whose subject is a statement line, not a payment
    (ledger `ORPHANED_SETTLEMENT`, reconciliation plan A3). Its `paymentId` is the line key
    `<paymentMessageId>#<lineNo>`, so the payment join finds nothing; without this row the
    queue would count it in `total` and never show it."""
    subject = (exc or {}).get("subjectRef") or {}
    if subject.get("kind") != "STATEMENT_LINE":
        return None
    detail = exc.get("detail") or {}
    return {
        "paymentId": key,
        "subjectRef": subject,
        "createdAt": exc.get("createdAt"),
        "amount": detail.get("actualAmount"),
        "currency": detail.get("currency"),
        "status": None,
        "direction": None,
    }


def list_exceptions(
    connection: MongoDBConnection,
    db_name: str,
    *,
    limit: int = 25,
    skip: int = 0,
    status: Optional[str] = "OPEN",
    category: Optional[str] = None,
) -> dict:
    """Payments that need intervention — the Operations lens (doc 24 §3 step 7).

    Driven by the `exceptions` collection, not by payment state: the exception is the
    intervention signal. The earlier `terminal state OR open exception` query never
    drained — every FAILED/RETURNED payment on the shared DB stayed listed after its
    exception was resolved — and it pulled every OPEN paymentId into an unbounded `$in`.

    `status` defaults to OPEN (the working queue); pass RESOLVED/DISMISSED for history or
    None for all. `category` narrows to one category (FR-9.IN4). Rows are ordered by the
    newest matching exception, one row per payment, each joined with its exception to
    render (`_pick_exception`: the OPEN one, else the latest).
    """
    coll = _payments(connection, db_name)
    exc_coll = connection.get_collection(db_name, "exceptions")

    # Plan B: backfilled precedents (`historical{}`) are agent evidence, not queue work.
    exc_query: dict = {"historical": None}
    if status:
        exc_query["status"] = status
    if category:
        exc_query["category"] = category

    # One row per payment, newest exception first. The matched set is the OPEN queue by
    # default — small by construction, since resolving removes a row.
    ordered_pids: list = []
    seen: set = set()
    for e in exc_coll.find(exc_query, {"_id": 0, "paymentId": 1, "createdAt": 1}).sort("createdAt", -1):
        pid = e.get("paymentId")
        if pid and pid not in seen:
            seen.add(pid)
            ordered_pids.append(pid)

    page_pids = ordered_pids[skip:skip + limit]
    by_pid = {
        p.get("paymentId"): p
        for p in coll.find({"paymentId": {"$in": page_pids}}, _LIST_PROJECTION)
    } if page_pids else {}
    joined = _join_exceptions(exc_coll, page_pids)
    items = []
    for pid in page_pids:
        if pid in by_pid:
            item = by_pid[pid]
        else:
            item = _statement_line_row(pid, joined.get(pid))
            if item is None:
                continue
        item["exception"] = joined.get(pid)
        items.append(item)
    return {
        "items": items,
        "total": len(ordered_pids),
        "limit": limit,
        "skip": skip,
        "status": status,
        "category": category,
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


def resolve_ref(connection: MongoDBConnection, db_name: str, ref: str) -> Optional[dict]:
    """Resolve a typed ref (``PAY-``/``TXN-``/``ACC-``/``NOTIF-``) to its parent paymentId.

    The back-office command search (research §3.1 #2) accepts any of the ``shared.refs``
    prefixes and deep-links to that payment's lifecycle. ``PAY-`` is the payment's own id;
    the others are secondary refs needing a lookup:

    * ``TXN-``  — ``payments.txnId`` (written at initiation, ``payment_document.py:160``).
    * ``ACC-``  — ``debtor.accountId`` **or** ``creditor.accountId``. An account has many
      payments, so the most recent one wins — the analyst is looking for a way in, not an
      exhaustive list.
    * ``NOTIF-`` — ``notifications.notificationId`` → its ``paymentId``.

    Returns ``{"paymentId", "matchedBy", "ref"}`` or ``None`` (router maps to 404). ``matchedBy``
    is the field the ref was found on, so the UI can say *"jumped via txnId"* and make a silent
    mismatch visible.
    """
    coll = _payments(connection, db_name)
    upper = (ref or "").upper()

    if upper.startswith("PAY-"):
        payment = coll.find_one({"paymentId": ref}, {"paymentId": 1, "_id": 0})
        return {"paymentId": payment["paymentId"], "matchedBy": "paymentId", "ref": ref} if payment else None

    if upper.startswith("TXN-"):
        payment = coll.find_one({"txnId": ref}, {"paymentId": 1, "_id": 0})
        return {"paymentId": payment["paymentId"], "matchedBy": "txnId", "ref": ref} if payment else None

    if upper.startswith("ACC-"):
        # Newest first — an account has many payments; the most recent is the way in.
        payment = coll.find_one(
            {"$or": [{"debtor.accountId": ref}, {"creditor.accountId": ref}]},
            {"paymentId": 1, "_id": 0},
            sort=[("createdAt", -1)],
        )
        return {"paymentId": payment["paymentId"], "matchedBy": "accountId", "ref": ref} if payment else None

    if upper.startswith("NOTIF-"):
        notif = connection.get_collection(db_name, "notifications").find_one(
            {"notificationId": ref}, {"paymentId": 1, "_id": 0}
        )
        if notif and notif.get("paymentId"):
            return {"paymentId": notif["paymentId"], "matchedBy": "notificationId", "ref": ref}
        return None

    return None
