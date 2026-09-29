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
    # idempotency.idempotencyKey is the caller's retry key, and UNIQUE here is load-bearing,
    # not an optimisation: capture's find_one pre-check cannot serialise concurrent identical
    # requests on its own, so without this constraint two racing callers both pass the
    # check and both move money. The DuplicateKeyError handler in capture.py is what
    # makes the second caller an idempotent replay — and it can only fire if this index
    # exists.
    #
    # Stage 1 (R7) moved this off `endToEndId`, which now carries the ISO 20022
    # EndToEndIdentification and nothing else. The 2026-09-22 Doina review relocated the
    # field from flat `idempotencyKey` to nested `idempotency.idempotencyKey` (her proposed
    # `idempotency{}` object) — the index path moved with it.
    #
    # ⚠️ PARTIAL, not sparse. `payment_document.build` initialises `idempotency:
    # {idempotencyKey: null, duplicateOf: null}` — the subdoc is PRESENT with a null leaf.
    # A unique *sparse* index on a nested path treats the path as present (the parent exists)
    # and indexes the null, so two payments with an unset key collide on the unique
    # constraint — the E11000 we hit on the first live Initiate after applying the sparse
    # index (2026-09-23). The FakeCollection `unique_on` emulation skips `None` (it checks
    # `val is not None`), so the hermetic suite did NOT catch this — a hermetic-vs-live
    # divergence. A partialFilterExpression on `$type: "string"` indexes only docs where
    # the key is an actual string, excluding both null and missing, which is the correct
    # shape for an optional nested retry key. (Same lesson as defects.md `fixture-fidelity` /
    # `repo-is-not-the-database`: the fake and the real server are not the same type system.)
    #
    # ⚠️ NOT YET APPLIED ON ATLAS — deferred to the end of the stage sequence by decision
    # (2026-08-30, doc 13 §6). Safe only because the index is INERT today: no caller sends
    # an `Idempotency-Key` header or an `idempotencyKey` field, so the value is always
    # null, both idempotency paths in capture.py are gated on it, and this partial index
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
        "keys": [("idempotency.idempotencyKey", ASCENDING)],
        "unique": True,
        "partialFilterExpression": {"idempotency.idempotencyKey": {"$type": "string"}},
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


# `routingSnapshots` is insert-only by design (Doina L500: immutable routing evidence). A
# unique index on the natural key is still right — a duplicate snapshot id would mean two
# routing decisions claiming to be the same one. (Stage 4b's commitment was folded into
# `payments.order` per Doina's Aug 27 target model, L427-429 — no `paymentOrders` index.)
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
    # The incoming flow makes one payment carry up to three messages (the received
    # pacs.008, the pacs.002 acknowledgement, and possibly a pacs.004 return), so the
    # deep-dive panel now selects BY PURPOSE rather than taking the only row. A compound
    # index on `(paymentId, purpose)` serves that; the `paymentId`-only index above still
    # serves the "all messages for this payment" read, which is the prefix.
    #
    # ⚠️ NOT partial on `purpose`: pre-incoming documents carry no `purpose` field at all,
    # and a partial index would exclude every one of them from a query that does not
    # constrain it — defect 2026-09-28 A6, where a partial index added for a write
    # invariant could not serve the read path on the same keys.
    {
        "name": "idx_payment_messages_payment_purpose",
        "keys": [("paymentId", ASCENDING), ("purpose", ASCENDING)],
    },
]

# Stage 9 — the `exceptions` queue. One OPEN occurrence per (paymentId, category) is the
# invariant `record_exception`'s dedupe relies on (doc 24 B3). A unique PARTIAL index on
# `(paymentId, category, status)` filtered to `status == "OPEN"` enforces it at the server:
# a concurrent writer that slips past the find_one pre-check (two reconciliation workers, or
# a duplicate-content race) hits E11000 and the catch in `record_exception` re-reads the
# winner. RESOLVED/DISMISSED rows are excluded from the partial filter, so a payment can
# accumulate resolved history (occurrence-per-doc, B2) without colliding on the unique key.
# Same partial-not-sparse lesson as `idx_idempotency_key_unique`: a `sparse` index on a
# present-nullable field would index the nulls and collide; the partialFilterExpression on
# the literal "OPEN" is the correct shape.
EXCEPTIONS_INDEXES = [
    {
        "name": "idx_exception_open_unique",
        "keys": [("paymentId", ASCENDING), ("category", ASCENDING), ("status", ASCENDING)],
        "unique": True,
        "partialFilterExpression": {"status": {"$eq": "OPEN"}},
    },
    # The read path. `_join_exceptions` resolves a whole page of Activity-list /
    # Operations-queue rows with one `{"paymentId": {"$in": [...]}}` query. The unique index
    # above cannot serve it: it is partial on `status == "OPEN"`, so a query that does not
    # constrain `status` is not eligible and the lookup collection-scans. `exceptions` only
    # grows — occurrence-per-doc (B2) never deletes, and the collection is shared — so this
    # is the 2026-08-31 shared-collection lesson applied to the read side.
    # `updatedAt` descending is part of the key because the join picks the
    # most-recently-updated row when none is OPEN.
    {
        "name": "idx_exception_payment_recent",
        "keys": [("paymentId", ASCENDING), ("updatedAt", DESCENDING)],
    },
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
        "routingSnapshots": _ensure(
            connection, db_name, "routingSnapshots", ROUTING_SNAPSHOTS_INDEXES
        ),
        "paymentExecutions": _ensure(
            connection, db_name, "paymentExecutions", PAYMENT_EXECUTIONS_INDEXES
        ),
        "paymentMessages": _ensure(
            connection, db_name, "paymentMessages", PAYMENT_MESSAGES_INDEXES
        ),
        "exceptions": _ensure(connection, db_name, "exceptions", EXCEPTIONS_INDEXES),
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
