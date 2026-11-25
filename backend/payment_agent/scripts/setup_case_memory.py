"""Set up the Reconciliation Agent's case memory on Atlas. Idempotent.

1. Creates the `exceptions_case_memory_vector` Vector Search index on `exceptions`
   (path `caseMemory.embedding`, filters `category` + `status`; skipped when it exists;
   Atlas builds it asynchronously).
2. Embeds every closed reconciliation exception that has no `caseMemory` yet — the
   backfilled HIST- precedents and any live case the agent closed before this existed.

Run from backend/payment_agent:  python -m scripts.setup_case_memory
Needs MONGODB_URI, LEAFYBANK_DB_NAME and VOYAGE_API_KEY (backend/payment_agent/.env).
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import case_memory  # noqa: E402
import policy  # noqa: E402
import semantic  # noqa: E402
from reconciliation_agent import _charge_bearer, _correspondent_bic  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("setup_case_memory")


def ensure_index(db) -> None:
    coll = db[case_memory.COLLECTION]
    if any(i["name"] == case_memory.INDEX_NAME for i in coll.list_search_indexes()):
        log.info("index %s exists", case_memory.INDEX_NAME)
        return
    coll.create_search_index(SearchIndexModel(
        definition=case_memory.index_definition(), name=case_memory.INDEX_NAME,
        type="vectorSearch"))
    log.info("index %s created (building on Atlas)", case_memory.INDEX_NAME)


def backfill(db) -> int:
    todo = list(db[case_memory.COLLECTION].find(
        {"category": {"$in": list(policy.WATCHED_CATEGORIES)},
         "status": {"$in": list(case_memory.CLOSED)},
         case_memory.FIELD: {"$exists": False}},
        {"_id": 0, case_memory.FIELD: 0}))
    indexed = 0
    for exc in todo:
        bic = (exc.get("historical") or {}).get("correspondentBic") or \
            _correspondent_bic(db, exc, exc.get("paymentId"))
        if case_memory.index_case(db, exc, correspondent_bic=bic,
                                  charge_bearer=_charge_bearer(db, exc)):
            indexed += 1
    log.info("embedded %d of %d closed exceptions without case memory", indexed, len(todo))
    return indexed


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    if not semantic.enabled():
        sys.exit("VOYAGE_API_KEY is not set.")
    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
    ensure_index(db)
    backfill(db)


if __name__ == "__main__":
    main()
