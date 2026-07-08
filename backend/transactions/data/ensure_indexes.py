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
from pymongo import ASCENDING

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

# payments.paymentId is the server-generated natural key (one doc per payment);
# transactions may hold several docs per paymentId (debit/credit legs), so its
# index is non-unique.
PAYMENTS_INDEXES = [
    {"name": "idx_payment_id", "keys": [("paymentId", ASCENDING)]},
]

TRANSACTIONS_INDEXES = [
    {"name": "idx_payment_id", "keys": [("paymentId", ASCENDING)]},
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
