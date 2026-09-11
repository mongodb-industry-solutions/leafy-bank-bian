#!/usr/bin/env python3
"""
Load the stage-2 corporate/DEBIT_BLOCK seed documents into a target database.

Doc 15 step 6 added four documents to backend/data/sample that no loader ever
imports — populate_leafy_bank_bian.py replays a mongodump snapshot (dump-core/),
which predates them. Without these four, stage 2's two most interesting paths have
nothing to run on:

  CUST-abc10001  ABC Manufacturing Inc.  CORPORATE / COMMERCIAL
  CUST-abc10002  Marcus Okonkwo          second signatory on ACC-acc10001
  ACC-acc10001   JOINT mandate, $250k    -> the $25,000 dual-approval scenario
  ACC-acc10002   active DEBIT_BLOCK      -> the account_unrestricted refusal

Scope is deliberately these four ids and nothing else. The target database is
shared across demos (558 customers for 9 accounts on fsi-bian-test-db), so a
whole-file load would be an unbounded write against documents we do not own.

Matched on the BUSINESS key (customerId / accountId), not _id: the seed files pin
_id, but an id-keyed upsert would silently create a duplicate business record if
one already existed under a different _id. _id is applied via $setOnInsert, so a
re-run never rewrites it.

Verified before writing this (2026-08-31): all four documents match the field
paths, BSON types and enum values of the documents already live — the only spec
deviation, interest.paymentAccountId, is present in all 11 seed accounts including
the 9 already loaded. Canonical spec at the time: v34_Aug17 (v4_34).

No index or validator work: customers/accounts carry only the automatic _id index
(see populate_leafy_bank_bian.py's collection notes), and the payments collection
validator stays unapplied per _state.md (gated on Doina Q9).

DRY-RUN by default. Nothing writes without --apply.

Usage (MONGODB_URI in env — same convention as the sibling populate script):
    export MONGODB_URI="mongodb+srv://..."
    python load_stage2_seed.py --db fsi-bian-test-db              # preview
    python load_stage2_seed.py --db fsi-bian-test-db --apply      # load
    python load_stage2_seed.py --db fsi-bian-test-db --verify     # check only

Idempotent: re-running reports "unchanged" once loaded.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bson.json_util import loads
from pymongo import MongoClient
from pymongo.errors import PyMongoError

SAMPLE_DIR = Path(__file__).resolve().parent / "sample"

# collection -> (business key, ids to load)
TARGETS = {
    "customers": ("customerId", ["CUST-abc10001", "CUST-abc10002"]),
    "accounts": ("accountId", ["ACC-acc10001", "ACC-acc10002"]),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def seed_docs(collection: str, key: str, ids: list[str]) -> list[dict]:
    """Read the wanted documents out of the sample file, as BSON-typed dicts."""
    path = SAMPLE_DIR / f"leafy_bank_bian.{collection}.json"
    docs = loads(path.read_text())          # json_util: resolves $oid / $date
    by_id = {d[key]: d for d in docs if key in d}

    missing = [i for i in ids if i not in by_id]
    if missing:
        raise KeyError(f"{path.name} has no {key} in {missing}")
    return [by_id[i] for i in ids]


def load(db, collection: str, key: str, ids: list[str], apply: bool) -> tuple[int, int]:
    """Upsert the named documents. Returns (inserted, already_present)."""
    coll = db[collection]
    inserted = present = 0

    for doc in seed_docs(collection, key, ids):
        ref = doc[key]
        existing = coll.find_one({key: ref}, {"_id": 1})
        if existing:
            present += 1
            log(f"  {ref:16s} already present (_id {existing['_id']}) — leaving alone")
            continue

        if not apply:
            log(f"  {ref:16s} would INSERT ({len(doc)} fields)")
            inserted += 1
            continue

        body = {k: v for k, v in doc.items() if k != "_id"}
        coll.update_one({key: ref}, {"$set": body, "$setOnInsert": {"_id": doc["_id"]}},
                        upsert=True)
        inserted += 1
        log(f"  {ref:16s} INSERTED")

    return inserted, present


def verify(db) -> bool:
    """Re-read what matters for the two stage-2 gate paths."""
    ok = True
    log("Verification:")

    for collection, (key, ids) in TARGETS.items():
        for ref in ids:
            doc = db[collection].find_one({key: ref})
            if not doc:
                log(f"  MISSING  {ref}")
                ok = False
                continue
            if collection == "customers":
                log(f"  {ref:16s} {doc['identification']['legalName']:26s} "
                    f"{doc['type']}/{doc['segment']}")
            else:
                rules = {s["signingRule"] for s in doc.get("signatories", [])}
                blocks = [r["type"] for r in doc.get("restrictions", [])]
                log(f"  {ref:16s} available={doc['balance']['available']:>10,.2f} "
                    f"signing={sorted(rules)} restrictions={blocks or 'none'}")

    # The dropdown the operator actually uses joins customer -> account, so an
    # orphan customer looks identical to a missing one in the UI.
    owned = db["accounts"].count_documents({"customerSnapshot.customerId": "CUST-abc10001"})
    log(f"\n  accounts owned by CUST-abc10001: {owned}"
        f"{'  <- 0 means the party stays hidden in the Initiate dropdown' if not owned else ''}")
    if not owned:
        ok = False

    return ok


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load the four stage-2 seed documents (corporate dual-approval + DEBIT_BLOCK).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", required=True,
                   help="target database (e.g. fsi-bian-test-db, leafy_bank_bian)")
    p.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    p.add_argument("--verify", action="store_true", help="verify only; no writes at all")
    args = p.parse_args()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        log("ERROR: MONGODB_URI is not set.")
        return 2

    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY-RUN")
    log(f"=== stage-2 seed load [{mode}] · db={args.db} ===\n")

    client = MongoClient(uri)
    try:
        db = client[args.db]

        if not args.verify:
            total_new = total_present = 0
            for collection, (key, ids) in TARGETS.items():
                log(f"{collection}:")
                new, present = load(db, collection, key, ids, args.apply)
                total_new += new
                total_present += present
                log("")
            log(f"  {total_new} to insert, {total_present} already present\n")

        if args.apply or args.verify:
            if not verify(db):
                log("\nVerification FAILED — see above.")
                return 1
    except (PyMongoError, KeyError) as e:
        log(f"ERROR: {e}")
        return 1
    finally:
        client.close()

    if args.apply or args.verify:
        log("\nDone. Next: the live gate — $25,000 INTERNAL from ACC-acc10001 must settle with\n"
            "dual_approval PASS and produce a journalEntries doc after the GL batch; a debit from\n"
            "ACC-acc10002 must be refused at account_unrestricted.")
    else:
        log("Done.  (dry-run — no writes; re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
