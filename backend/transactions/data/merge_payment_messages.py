"""One-time copy: `paymentMessages` -> `canonicalJsonStorage`.

Run from ``backend/transactions``::

    python -m data.merge_payment_messages            # dry run: counts and conflicts only
    python -m data.merge_payment_messages --apply    # copy
    python -m data.merge_payment_messages --apply --drop-source   # copy, then drop the source

`canonicalJsonStorage` already holds fsi-payments-processing's conversion records, so
`renameCollection` is wrong here: it fails without `dropTarget` and destroys their documents
with it. This copies instead, never touches existing target documents, and is safe to re-run
(a document whose `_id` is already in the target is skipped).

Order of use on each environment:
  1. ``python -m data.ensure_indexes`` (creates the partial unique indexes on the target first).
  2. This script with ``--apply``.
  3. Deploy the build that writes to `canonicalJsonStorage`; verify.
  4. This script with ``--apply --drop-source`` once the build is verified. The drop is refused
     unless every source `_id` is present in the target.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from pymongo.errors import BulkWriteError

from database.connection import MongoDBConnection

SOURCE = "paymentMessages"
TARGET = "canonicalJsonStorage"
BATCH = 500


def _missing_ids(source, target) -> list:
    """Source `_id`s not yet in the target, in source order."""
    missing = []
    for doc in source.find({}, {"_id": 1}):
        if target.count_documents({"_id": doc["_id"]}, limit=1) == 0:
            missing.append(doc["_id"])
    return missing


def merge(source, target, *, apply: bool, drop_source: bool = False) -> int:
    """Copy the source into the target. Returns the exit code: 1 on any failure, else 0."""
    source_count = source.count_documents({})
    target_count = target.count_documents({})
    missing = _missing_ids(source, target)
    print(f"{SOURCE}: {source_count} document(s). {TARGET}: {target_count} document(s).")
    print(f"{len(missing)} source document(s) not yet in {TARGET}; "
          f"{source_count - len(missing)} already there (same _id).")

    if apply and missing:
        for start in range(0, len(missing), BATCH):
            ids = missing[start:start + BATCH]
            docs = list(source.find({"_id": {"$in": ids}}))
            try:
                target.insert_many(docs, ordered=False)
            except BulkWriteError as exc:
                print(f"error: insert failed: {exc.details.get('writeErrors', [])[:3]}")
                return 1
        missing = _missing_ids(source, target)
        print(f"Copied. {len(missing)} source document(s) still missing from {TARGET}.")
        if missing:
            return 1
    elif not apply:
        print("\nDry run. Re-run with --apply to copy.")
        return 0

    if drop_source:
        if missing:
            print(f"error: refusing to drop {SOURCE}; documents are missing from {TARGET}.")
            return 1
        source.drop()
        print(f"Dropped {SOURCE}.")
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="copy; default is a dry run")
    parser.add_argument("--drop-source", action="store_true",
                        help="drop the source afterwards (requires --apply)")
    args = parser.parse_args(argv)
    if args.drop_source and not args.apply:
        parser.error("--drop-source requires --apply")

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set. Create backend/transactions/.env (see README).")
    connection = MongoDBConnection(uri)
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
    sys.exit(merge(connection.get_collection(db_name, SOURCE),
                   connection.get_collection(db_name, TARGET),
                   apply=args.apply, drop_source=args.drop_source))


if __name__ == "__main__":
    main()
