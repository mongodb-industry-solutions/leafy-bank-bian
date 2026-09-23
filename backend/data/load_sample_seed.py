#!/usr/bin/env python3
"""
Load the canonical sample seed (`backend/data/sample/`) into a target database.

This is the scripted replacement for the README's "import each file with Compass or
mongoimport" step: one command loads all four collections the demo ships,
upserting by business key so a re-run after a later stage lands does not fail on
duplicates and does not need the operator to hand-pick new rows.

    export MONGODB_URI="mongodb+srv://..."
    python load_sample_seed.py --db leafy_bank_bian              # dry-run
    python load_sample_seed.py --db leafy_bank_bian --apply      # load
    python load_sample_seed.py --db leafy_bank_bian --verify     # check only

Idempotent: re-running reports UNCHANGED once loaded; new rows (e.g. stage 7's
`ACC-CLEARING-WIRE` + the 1130/1131/1132/1139 CoA leaves) insert, and fields a
later stage introduced onto an existing sample doc get applied additively.

## Upsert semantics (why not a blanket `update_one(..., upsert=True)`)

The target DB is shared across demos (`leafy_bank_bian`), and these collections are
not pure reference data — `accounts.balance` is `$inc`'d on every payment and the
clearing account's position moves as wires settle. So per row, by business key:

- **missing**                  → INSERT (via `$setOnInsert` of the pinned `_id`)
- **present, ours**            → apply only fields the stored doc LACKS (new columns
                                a later stage added). Existing values are left alone,
                                so runtime balance/state is never clobbered.
                                `--refresh` forces a full `$set` of the sample body —
                                documented hazard: it resets runtime-mutated fields.
- **present under a foreign `_id`**  → SKIP + loud warning, never overwrite. The
                                sample pins `_id`, and the older loaders (and this
                                script) always used it, so an `_id` outside the
                                sample's declared set means a row another demo owns
                                collides on this business key. This is the
                                `repo-is-not-the-database` prevention rule encoded:
                                a repo fact (the sample) does not establish
                                ownership, and a business-key match is not a license
                                to overwrite.

DRY-RUN by default. Nothing writes without `--apply`.
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

# collection -> business key (the field a re-run must match on)
TARGETS = {
    "customers": "customerId",
    "accounts": "accountId",
    "transactions": "txnId",
    "glAccounts": "accountCode",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def sample_docs(collection: str) -> list[dict]:
    """Read one sample file as BSON-typed dicts (`json_util` resolves $oid/$date)."""
    path = SAMPLE_DIR / f"leafy_bank_bian.{collection}.json"
    docs = loads(path.read_text())
    if not isinstance(docs, list) or not docs:
        raise KeyError(f"{path.name}: expected a non-empty array")
    return docs


def declared_ids(docs: list[dict]) -> set[str]:
    """The `_id`s the sample pins — proof of ownership when matching a live row."""
    return {str(d["_id"]) for d in docs if "_id" in d}


def classify(doc: dict, existing: dict | None, declared: set[str]) -> tuple[str, list[str], list[str]]:
    """Return (kind, new_fields, changed_fields).

    kinds: INSERT | UPDATE | DIFFERS | UNCHANGED | SKIP
    - new_fields    = sample keys the live row lacks (apply these additively)
    - changed_fields = sample keys whose VALUE differs from the live row (leave alone)
    """
    if existing is None:
        return "INSERT", [], []

    if str(existing["_id"]) not in declared:
        return "SKIP", [], []

    new_fields = [k for k in doc if k != "_id" and k not in existing]
    changed_fields = [
        k for k in doc if k != "_id" and k in existing and existing[k] != doc[k]
    ]

    if new_fields:
        return "UPDATE", new_fields, changed_fields
    if changed_fields:
        return "DIFFERS", [], changed_fields
    return "UNCHANGED", [], []


def load(db, collection: str, key: str, docs: list[dict], declared: set[str],
         apply: bool, refresh: bool) -> dict[str, int]:
    """Reconcile one collection. Returns {inserted, updated, differs, unchanged, skipped}."""
    coll = db[collection]
    stats = {"inserted": 0, "updated": 0, "differs": 0, "unchanged": 0, "skipped": 0}

    # Ownership is asserted by the pinned `_id` (see module docstring), so a sample
    # row without one breaks the contract — fail loudly, not silently as a "foreign" row.
    unpinned = [d.get(key, "?") for d in docs if "_id" not in d]
    if unpinned:
        raise KeyError(f"{collection}: {len(unpinned)} row(s) without a pinned _id: {unpinned}")

    seen: set[str] = set()
    for doc in docs:
        ref = doc.get(key)
        if ref is None:
            raise KeyError(f"{collection}: a sample doc has no '{key}'")
        if ref in seen:
            log(f"  WARN  duplicate {key} {ref!r} in sample — last wins")
        seen.add(ref)

        existing = coll.find_one({key: ref})
        kind, new_fields, changed_fields = classify(doc, existing, declared)
        ref_s = f"{ref:<16s}"

        if kind == "INSERT":
            if apply:
                body = {k: v for k, v in doc.items() if k != "_id"}
                coll.update_one({key: ref},
                                {"$set": body, "$setOnInsert": {"_id": doc["_id"]}},
                                upsert=True)
            stats["inserted"] += 1
            verb = "INSERTED" if apply else "would INSERT"
            log(f"  {ref_s} {verb} ({len(doc)} fields)")

        elif kind in ("UPDATE", "DIFFERS"):
            if refresh:
                # Full overwrite of an owned row — documented hazard: resets
                # runtime-mutated fields (balances, settlement positions), so it is
                # opt-in, never the default.
                if apply:
                    body = {k: v for k, v in doc.items() if k != "_id"}
                    coll.update_one({"_id": existing["_id"]}, {"$set": body})
                stats["updated"] += 1
                log(f"  {ref_s} REFRESHED ({len(doc)} fields)")

            elif kind == "UPDATE":
                # Additive: apply only columns a later stage introduced. Existing
                # values — including runtime balances — are never touched.
                if apply:
                    body = {k: doc[k] for k in new_fields}
                    coll.update_one({"_id": existing["_id"]}, {"$set": body})
                stats["updated"] += 1
                log(f"  {ref_s} ADDED {new_fields}")

            else:  # DIFFERS, no refresh
                stats["differs"] += 1
                log(f"  {ref_s} DIFFERS on {changed_fields} — left untouched "
                    "(use --refresh to force)")

        elif kind == "SKIP":
            stats["skipped"] += 1
            log(f"  {ref_s} SKIP — live row _id {existing['_id']} not in sample's set; "
                "not ours, not overwritten")

        else:  # UNCHANGED
            stats["unchanged"] += 1
            log(f"  {ref_s} unchanged")

    coll_notes = {"inserted": stats["inserted"], "updated": stats["updated"],
                  "differs": stats["differs"], "skipped": stats["skipped"]}
    log(f"  -> {coll_notes}")
    return stats


def verify(db, collection: str, key: str, docs: list[dict], declared: set[str]) -> tuple[bool, int]:
    """Spot-check each sample row is loadable and present by business key."""
    ok = True
    coll = db[collection]
    missing = present = 0
    for doc in docs:
        ref = doc.get(key)
        existing = coll.find_one({key: ref}, {"_id": 1})
        if existing is None:
            missing += 1
            log(f"  MISSING  {ref}")
            ok = False
        else:
            present += 1
            if str(existing["_id"]) not in declared:
                log(f"  foreign {ref} (_id {existing['_id']} not in sample set)")
                ok = False
    log(f"  {collection}: {present} present, {missing} missing, {len(docs)} in sample")
    return ok, missing


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load the canonical sample seed into a target database (upsert by business key).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", required=True,
                   help="target database (e.g. leafy_bank_bian)")
    p.add_argument("--collections", default=",".join(TARGETS),
                   help=f"comma-separated subset of {list(TARGETS)} (default: all)")
    p.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    p.add_argument("--refresh", action="store_true",
                   help="full $set of the sample body on owned rows (clobbers runtime "
                        "balances/state; additive-only is the default)")
    p.add_argument("--verify", action="store_true", help="verify only; no writes at all")
    args = p.parse_args()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        log("ERROR: MONGODB_URI is not set.")
        return 2

    wanted = [c.strip() for c in args.collections.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in TARGETS]
    if unknown:
        log(f"ERROR: unknown collections {unknown}")
        return 2

    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY-RUN")
    log(f"=== sample seed load [{mode}] · db={args.db} · {wanted} ===\n")

    client = MongoClient(uri)
    try:
        db = client[args.db]
        failed = False

        for collection in wanted:
            key = TARGETS[collection]
            docs = sample_docs(collection)
            declared = declared_ids(docs)
            log(f"{collection} ({len(docs)} rows, key={key}):")

            if args.verify:
                ok, missing = verify(db, collection, key, docs, declared)
                failed = failed or not ok or missing
                log("")
                continue

            stats = load(db, collection, key, docs, declared, args.apply, args.refresh)
            failed = failed or stats["skipped"] > 0
            log(f"  {sum(stats.values())} rows reconciled\n")

        if failed:
            log("\nResult: DONE with foreign-row skips or failures — inspect the lines above.")
            return 1
    except (PyMongoError, KeyError) as e:
        log(f"ERROR: {e}")
        return 1
    finally:
        client.close()

    if not (args.apply or args.verify):
        log("Done.  (dry-run — no writes; re-run with --apply)")
    else:
        log("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
