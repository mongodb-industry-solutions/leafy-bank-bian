"""Idempotent index creator for the transactions service's collections.

Run: ``python -m data.ensure_indexes`` from ``backend/transactions``.

``create_index`` is idempotent — re-running is a no-op when the index already
matches. This script does not seed data.

Both collections are keyed by paymentId in the service's own reads
(transactions_service.get_transactions, payments_service.retrieve_payment) and
in the ledger's trace_payment monitor query, which previously ran a COLLSCAN on
every trace poll.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

# payments.paymentId is the server-generated natural key (one doc per payment);
# transactions may hold several docs per paymentId (debit/credit legs), so its
# index is non-unique.
PAYMENTS_INDEXES = [
    {"name": "idx_payment_id", "keys": [("paymentId", ASCENDING)]},
    # The back-office workflow list sorts newest-first and filters by status, so createdAt
    # leads and status follows: this serves both the unfiltered list (sort alone) and the
    # status-filtered one. Doc 16 B2 asserted `idx_payments_customerId_createdAt` and
    # `idx_payments_debtorAccount_status` already existed — neither ever did; this is the
    # index that claim needed. Without it the list COLLSCANs and blocking-sorts `payments`.
    {
        "name": "idx_payments_createdAt_status",
        "keys": [("createdAt", DESCENDING), ("status", ASCENDING)],
    },
    # idempotencyKey is the caller's retry key, and UNIQUE here is load-bearing, not an
    # optimisation: capture's find_one pre-check cannot serialise concurrent identical
    # requests on its own, so without this constraint two racing callers both pass the
    # check and both move money. The DuplicateKeyError handler in capture.py is what
    # makes the second caller an idempotent replay — and it can only fire if this index
    # exists. SPARSE because the field is legitimately null for any payment initiated
    # without a retry key; a plain unique index would let exactly one such payment exist.
    #
    # Stage 1 (R7) moved this off `endToEndId`, which now carries the ISO 20022
    # EndToEndIdentification and nothing else.
    #
    # ⚠️ NOT YET APPLIED ON ATLAS — deferred to the end of the stage sequence by decision
    # (2026-08-30, doc 13 §6). Safe only because the index is INERT today: no caller sends
    # an `Idempotency-Key` header or an `idempotencyKey` field, so the value is always
    # null, both idempotency paths in capture.py are gated on it, and a sparse index
    # constrains nothing.
    #
    # **Precondition — do not make any caller send an idempotency key before running this.**
    # capture.py's find_one pre-check would then dedupe sequential retries and look correct
    # while two concurrent requests with the same key both moved money. If you add a retry
    # wrapper, a load test, or the header, apply this index in the same change.
    #
    # Also for the Atlas run: `create_index` never drops, so the old
    # `idx_end_to_end_id_unique` survives until dropped by hand. Harmless (endToEndId is
    # server-derived and unique per payment) but dead weight — drop it then.
    {
        "name": "idx_idempotency_key_unique",
        "keys": [("idempotencyKey", ASCENDING)],
        "unique": True,
        "sparse": True,
    },
]

# `transactions` is shared with the ThreatSight 360 demo, which adds ~21k docs stamped
# sourceSystem "threatsight360". The three indexes below keep Leafy Bank's own reads
# independent of that volume; without them each read COLLSCANned the whole collection
# and blocking-sorted it just to return `limit` rows.
#
# Deliberately NOT partial indexes on sourceSystem: an equality seek on an account id
# already bounds the scan to matching entries (T360 docs carry their own account ids and
# are never visited), so a partialFilterExpression would only save index bytes while
# adding a filter/query coupling that fails silently if the two drift apart.
TRANSACTIONS_INDEXES = [
    {"name": "idx_payment_id", "keys": [("paymentId", ASCENDING)]},
    # accounts_service.get_recent_activity matches ownership with an $or over both
    # sides, so each branch needs its own index; the sort keys trail the seek key so
    # the planner can SORT_MERGE the branches and honour `limit` with no blocking sort.
    {"name": "idx_payer_account_booking", "keys": [
        ("payer.accountId", ASCENDING), ("bookingDate", DESCENDING), ("_id", DESCENDING),
    ]},
    {"name": "idx_payee_account_booking", "keys": [
        ("payee.accountId", ASCENDING), ("bookingDate", DESCENDING), ("_id", DESCENDING),
    ]},
    # pipeline_read_service.list_transactions has no account to seek on — sourceSystem
    # is its only filter, so it leads here, with createdAt trailing to serve the sort
    # and the accompanying count_documents straight from the index.
    {"name": "idx_source_system_created", "keys": [
        ("sourceSystem", ASCENDING), ("createdAt", DESCENDING),
    ]},
]


# Stage 4's two collections (doc 18 B1). Neither is in the canonical spec, so neither has a
# spec-declared index list either — these follow the same shape as `payments`: the natural
# key unique, plus `paymentId` for the trace read path.
#
# ⚠️ NOT YET APPLIED ON ATLAS. Same deferral as `idx_idempotency_key_unique` above
# (2026-08-30): the whole Atlas index run waits until the stage sequence is built. Safe,
# because neither collection is read by anything yet — the forward pointers on `payments`
# are what the trace uses — and `insert_one` needs no index to be correct.
PAYMENT_ORDERS_INDEXES = [
    {
        "name": "idx_payment_order_id_unique",
        "keys": [("paymentOrderId", ASCENDING)],
        "unique": True,
    },
    {"name": "idx_payment_orders_payment_id", "keys": [("paymentId", ASCENDING)]},
]

# `routingSnapshots` is insert-only by design (Doina L500: immutable routing evidence). A
# unique index on the natural key is still right — a duplicate snapshot id would mean two
# routing decisions claiming to be the same one.
ROUTING_SNAPSHOTS_INDEXES = [
    {
        "name": "idx_routing_snapshot_id_unique",
        "keys": [("routingSnapshotId", ASCENDING)],
        "unique": True,
    },
    {"name": "idx_routing_snapshots_payment_id", "keys": [("paymentId", ASCENDING)]},
]

# Stage 5's two collections (doc 19 B3, B7). Same situation as stage 4's: neither is in the
# canonical spec, so neither carries a spec-declared index list.
#
# `paymentExecutions` is append-only per *attempt*, so `(paymentId, attempt)` is the natural
# compound read key — "every attempt for this payment, in order" is the query stage 9's repair
# view will make. NOT unique: it would be correct today, but a unique constraint on an
# append-only artifact is the kind of thing that turns a future concurrent retry into a lost
# execution record rather than a duplicate one.
PAYMENT_EXECUTIONS_INDEXES = [
    {
        "name": "idx_payment_execution_id_unique",
        "keys": [("paymentExecutionId", ASCENDING)],
        "unique": True,
    },
    {
        "name": "idx_payment_executions_payment_attempt",
        "keys": [("paymentId", ASCENDING), ("attempt", ASCENDING)],
    },
]

# `paymentMessages` — Doina's rename (L780). Insert-only: nothing updates a message record,
# because a re-mapping is a new attempt with a new message.
#
# ⚠️ **No index is declared for `canonicalJsonStorage`** and none must be. That collection is
# fsi-payments-processing's, live with 33-34 documents on `ist-shared.leafy_bank_bian`, and its
# spec-declared indexes (unique `id`, unique sparse `jsonData.transactionRef`) have never been
# applied there — creating one could fail on their rows or start rejecting their inserts. Doc
# 19 B7.
PAYMENT_MESSAGES_INDEXES = [
    {
        "name": "idx_payment_message_id_unique",
        "keys": [("paymentMessageId", ASCENDING)],
        "unique": True,
    },
    {"name": "idx_payment_messages_payment_id", "keys": [("paymentId", ASCENDING)]},
]


def _ensure(connection: MongoDBConnection, db_name: str, collection: str, specs: list[dict]) -> list[str]:
    coll = connection.get_collection(db_name, collection)
    ensured = []
    for spec in specs:
        opts = {k: v for k, v in spec.items() if k not in ("name", "keys")}
        coll.create_index(spec["keys"], name=spec["name"], **opts)
        ensured.append(spec["name"])
    return ensured


def ensure_transactions_indexes(connection: MongoDBConnection, db_name: str) -> dict[str, list[str]]:
    """Ensure indexes for the transactions service's collections. Returns a collection→names map."""
    return {
        "payments": _ensure(connection, db_name, "payments", PAYMENTS_INDEXES),
        "transactions": _ensure(connection, db_name, "transactions", TRANSACTIONS_INDEXES),
        "paymentOrders": _ensure(
            connection, db_name, "paymentOrders", PAYMENT_ORDERS_INDEXES
        ),
        "routingSnapshots": _ensure(
            connection, db_name, "routingSnapshots", ROUTING_SNAPSHOTS_INDEXES
        ),
        "paymentExecutions": _ensure(
            connection, db_name, "paymentExecutions", PAYMENT_EXECUTIONS_INDEXES
        ),
        "paymentMessages": _ensure(
            connection, db_name, "paymentMessages", PAYMENT_MESSAGES_INDEXES
        ),
    }


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set. Create backend/transactions/.env (see README).")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    connection = MongoDBConnection(uri)
    results = ensure_transactions_indexes(connection, db_name)
    for coll, names in results.items():
        logger.info("%s indexes ensured on %s: %s", coll, db_name, names)


if __name__ == "__main__":
    main()
