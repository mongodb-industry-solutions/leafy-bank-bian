#!/usr/bin/env python3
"""
Load stage-3's two reference collections into a target database. Doc 17 §3 step 1.

  purposeCodes         15 ISO 20022 ExternalPurpose codes, keyed on `code`
  correspondentBanks    7 BIC_DIRECTORY institution rows, keyed on `swiftCode`

Stage 3 enrichment resolves the creditor's bank identity and clearing member id, and
validates the purpose code, through `ReferenceData`. Neither collection had a seed or a
code reference in this repo (doc 17 B2/B3).

⚠️ **Corrected 2026-08-31, first dry run:** all 15 `purposeCodes` were **already present on
fsi-bian-test-db**, seeded by another demo. Doc 17 B3 reasoned from the collection's absence
in this REPO and treated that as absence in the DATABASE — not the same fact. The loader is
keyed on `code`, so it left every row alone; this file's purposeCodes seed is now a
belt-and-braces fallback for a fresh database, not the primary source. `verify()` therefore
checks the shape of whatever is there rather than assuming it is ours.
`correspondentBanks` genuinely has no row for any bank this demo sends to — it covers only
JP/IN corridors.

READ-ONLY AT RUNTIME. The service never writes these collections — seeding is this script,
run by hand. The target database is shared across demos (558 customers for 9 accounts on
fsi-bian-test-db), so an app-managed write is how you corrupt another demo's data.

Scope is deliberately the 22 documents in backend/data/seed and nothing else. Matched on
the BUSINESS key (`code` / `swiftCode`), not `_id`, and the seed files pin no `_id` at all
— Mongo assigns one on insert. An id-keyed upsert would silently create a duplicate
business record if one already existed under a different `_id` (same reasoning as
load_stage2_seed.py).

correspondentBanks is scoped by `recordType: "BIC_DIRECTORY"` on every query, insert and
count. JP_ENTITY_BANK / IN_BRANCH rows in the same collection belong to another demo and
are never read, written, or counted here.

⚠️ Before --apply: the DB user needs **readWrite** on the target database. `dbAdmin` grants
index/schema admin but no find/insert, so a service boots clean and then fails at runtime
— defect 2026-06-12 (`db-user-grant-scope`), which cost a staging cutover.

⚠️ Doc 17 Q22 is unratified. `recordType: "BIC_DIRECTORY"` and the
clearingSystemCode / clearingSystemMemberId fields are NOT in the canonical spec. If Doina
prefers a separate collection, only this file's TARGETS entry and the seed file change —
no consumer code moves.

No index work: `purposeCodes.idx_code` (unique) and `correspondentBanks.idx_swift_code`
(sparse) are declared in the canonical spec, and applying indexes to a shared database is
a separate, deliberate operation (see _state.md on the deferred Atlas index work). At 22
documents a collection scan is sub-millisecond — defect 2026-07-08: establish row counts
before treating an index as a performance fix.

DRY-RUN by default. Nothing writes without --apply.

Usage (MONGODB_URI in env — same convention as the sibling loaders):
    export MONGODB_URI="mongodb+srv://..."
    python load_reference_seed.py --db fsi-bian-test-db            # preview
    python load_reference_seed.py --db fsi-bian-test-db --apply    # load + verify
    python load_reference_seed.py --db fsi-bian-test-db --verify   # check only

Idempotent: re-running reports every row "already present" and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError

SEED_DIR = Path(__file__).resolve().parent / "seed"

BIC_DIRECTORY = "BIC_DIRECTORY"

# collection -> (business key, extra query scope applied to every read/write/count)
TARGETS = {
    "purposeCodes": ("code", {}),
    "correspondentBanks": ("swiftCode", {"recordType": BIC_DIRECTORY}),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def seed_records(collection: str) -> list[dict]:
    """Read `records` out of a seed file, ignoring the `_notes` block.

    The files are `{"_notes": {...}, "records": [...]}` rather than a bare array so the
    provenance notes can never be mistaken for a document to load.
    """
    path = SEED_DIR / f"leafy_bank_bian.{collection}.json"
    payload = json.loads(path.read_text())
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise KeyError(f"{path.name} has no non-empty 'records' array")
    return records


def load(db, collection: str, key: str, scope: dict, apply: bool) -> tuple[int, int]:
    """Upsert every seed record. Returns (inserted, already_present)."""
    coll = db[collection]
    inserted = present = 0

    for doc in seed_records(collection):
        if key not in doc:
            raise KeyError(f"{collection}: a seed record has no '{key}'")
        ref = doc[key]
        query = {key: ref, **scope}

        if coll.find_one(query, {"_id": 1}):
            present += 1
            log(f"  {ref:12s} already present — leaving alone")
            continue

        if not apply:
            inserted += 1
            log(f"  {ref:12s} would INSERT ({len(doc)} fields)")
            continue

        coll.update_one(query, {"$set": doc}, upsert=True)
        inserted += 1
        log(f"  {ref:12s} INSERTED")

    return inserted, present


def verify(db) -> bool:
    """Re-read what stage-3 enrichment will actually query."""
    ok = True
    log("Verification:")

    # purposeCodes — SUPP is the code in Doina's own sample and demo script.
    #
    # NOTE (2026-08-31): this collection was ALREADY populated on fsi-bian-test-db by
    # another demo, so these rows may not be ours. The adapter projects
    # code/name/description/category, so the shape is checked here rather than assumed —
    # doc 17 B3 reasoned from the collection's absence in THIS REPO, which is not the same
    # fact as its absence in the database.
    rows = list(
        db["purposeCodes"].find(
            {}, {"_id": 0, "code": 1, "name": 1, "description": 1, "category": 1}
        )
    )
    codes = sorted(r["code"] for r in rows if "code" in r)
    log(f"  purposeCodes: {len(rows)} rows — {', '.join(codes)}")
    if "SUPP" not in codes:
        log("  MISSING SUPP — the flagship wire's categoryPurpose cannot resolve")
        ok = False

    # Field coverage. The adapter degrades (empty strings) rather than raising on a partial
    # row, so a gap here is a display problem, not an outage — report it, do not fail.
    for field in ("name", "description", "category"):
        absent = sorted(r["code"] for r in rows if not r.get(field))
        if absent:
            log(f"    NOTE: {len(absent)} row(s) have no '{field}': {', '.join(absent)}")

    # `embedding` is deliberately not seeded by us (doc 17 §6 defers semantic matching).
    # If rows carry one, another demo owns them — worth knowing before editing any.
    with_embedding = db["purposeCodes"].count_documents({"embedding": {"$exists": True}})
    if with_embedding:
        log(f"    NOTE: {with_embedding} row(s) carry an `embedding` — seeded by another "
            "demo, not by this loader. Never overwritten (upsert is keyed on `code` and "
            "skips existing rows).")

    # correspondentBanks — only our own record type.
    scope = {"recordType": BIC_DIRECTORY}
    banks = list(
        db["correspondentBanks"].find(
            scope,
            {"_id": 0, "swiftCode": 1, "bankName": 1, "country": 1,
             "clearingSystemCode": 1, "clearingSystemMemberId": 1},
        )
    )
    log(f"\n  correspondentBanks[{BIC_DIRECTORY}]: {len(banks)} rows")
    for b in sorted(banks, key=lambda x: x.get("swiftCode", "")):
        log(f"    {b.get('swiftCode',''):10s} {b.get('country',''):2s} "
            f"{(b.get('clearingSystemCode') or '-'):6s} "
            f"{(b.get('clearingSystemMemberId') or '-'):>10s}  {b.get('bankName','')}")

    by_bic = {b.get("swiftCode") for b in banks}
    # LEAFUS33 is us: without it our own outbound wire has no ABA, which is the exact
    # reason D10 called the wire_domestic sample unsendable. CHASUS33 is her sample's
    # beneficiary.
    for required in ("LEAFUS33", "CHASUS33"):
        if required not in by_bic:
            log(f"  MISSING {required}")
            ok = False

    # The autofill pool and this seed must stay in step, or every autopopulated wire WARNs.
    autofill = {"CHASUS33", "CITIUS33", "BARCGB22", "DEUTDEFF", "UBSWCHZH", "ROYCCAT2"}
    gaps = sorted(autofill - by_bic)
    if gaps:
        log(f"  WARNING: autofill BICs with no directory row: {', '.join(gaps)}"
            "\n           (InitiateWizard.js EXTERNAL_BANKS — those wires will log a miss)")

    # Other demos' rows, reported so a surprise is visible rather than silent.
    others = db["correspondentBanks"].count_documents({"recordType": {"$ne": BIC_DIRECTORY}})
    if others:
        log(f"\n  ({others} non-{BIC_DIRECTORY} rows in correspondentBanks — another "
            "demo's JP/IN records, untouched)")

    return ok


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load stage-3 reference data (purposeCodes + BIC_DIRECTORY banks).",
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
    log(f"=== stage-3 reference seed [{mode}] · db={args.db} ===\n")

    client = MongoClient(uri)
    try:
        db = client[args.db]

        if not args.verify:
            total_new = total_present = 0
            for collection, (key, scope) in TARGETS.items():
                log(f"{collection}:")
                new, present = load(db, collection, key, scope, args.apply)
                total_new += new
                total_present += present
                log("")
            log(f"  {total_new} to insert, {total_present} already present\n")

        if args.apply or args.verify:
            if not verify(db):
                log("\nVerification FAILED — see above.")
                return 1
    except (PyMongoError, KeyError, json.JSONDecodeError) as e:
        log(f"ERROR: {e}")
        return 1
    finally:
        client.close()

    if args.apply or args.verify:
        log("\nDone. Reference data is read-only at runtime; the service never writes it.")
    else:
        log("Done.  (dry-run — no writes; re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
