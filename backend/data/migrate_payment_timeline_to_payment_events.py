#!/usr/bin/env python3
"""Copy every document from `paymentTimeline` into `paymentEvents`.

MongoDB cannot rename a collection here, so the data is copied and the old collection is left
in place as the rollback. Run from `backend/` with `MONGODB_URI` (and optionally
`LEAFYBANK_DB_NAME`, default `leafy_bank_bian`) set:

    python data/migrate_payment_timeline_to_payment_events.py            # dry-run (default)
    python data/migrate_payment_timeline_to_payment_events.py --apply

Order of a release: `python -m data.ensure_indexes` from `backend/transactions` (creates `paymentEvents` with its validator
and index), stop writers, `--apply`, deploy the new build, then confirm the printed counts match.
Idempotent: documents are keyed by `_id` and inserted only when missing, so a second run copies
whatever was written to `paymentTimeline` since the first. Existing `paymentEvents` documents are
never overwritten. `paymentTimeline` is never modified; drop it by hand once the new build is verified.
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

SOURCE = "paymentTimeline"
TARGET = "paymentEvents"


def _setoninsert(doc: dict) -> UpdateOne:
    body = {k: v for k, v in doc.items() if k != "_id"}
    return UpdateOne({"_id": doc["_id"]}, {"$setOnInsert": body}, upsert=True)


def copy_collection(db, apply: bool, batch_size: int) -> tuple[int, int]:
    """Copy `SOURCE` into `TARGET`. Returns `(inserted, already_present)`."""
    source_count = db[SOURCE].count_documents({})
    print(f"{SOURCE}: {source_count} document(s) [{'APPLY' if apply else 'DRY-RUN'}]")
    if not apply:
        existing = db[TARGET].count_documents({})
        print(f"{TARGET}: {existing} document(s) already present; nothing written")
        return 0, existing

    if TARGET not in db.list_collection_names():
        raise SystemExit(f"{TARGET} does not exist; run `python -m data.ensure_indexes` from backend/transactions first")

    inserted = 0
    ops = []
    for doc in db[SOURCE].find({}):
        ops.append(_setoninsert(doc))
        if len(ops) >= batch_size:
            inserted += db[TARGET].bulk_write(ops, ordered=False).upserted_count
            ops = []
    if ops:
        inserted += db[TARGET].bulk_write(ops, ordered=False).upserted_count

    target_count = db[TARGET].count_documents({})
    print(f"inserted {inserted}; {TARGET} now holds {target_count}")
    return inserted, source_count - inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args()
    load_dotenv(".env")
    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
    copy_collection(db, args.apply, args.batch)
    if args.apply:
        missing = db[SOURCE].count_documents({}) - db[TARGET].count_documents({})
        if missing > 0:
            print(f"ERROR: {TARGET} is short by {missing} document(s)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
