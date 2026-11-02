#!/usr/bin/env python3
"""Load the cutoff demo's seeded world (cutoff plan Part B) into a target database.

Writes what `cutoff_seed` builds — nothing else:

  staffDirectory      Maya (OOO), Raj, Lena, analysts 1-3          upsert by staffId
  accounts            ACC-acc10003 (6,300 + an expected credit)     $setOnInsert by accountId
                      Maya + Raj joined to ACC-acc10001's mandate  pushed only if absent
  cutoffWorkflow      Lena's six synthetic OPEN approval requests  upsert by approvalRequestId

The database is shared with other demos, so the loader updates only documents it owns
(`sourceSystem: cutoff-demo-seed`): an existing account is never rewritten, and
a staff id or request id already owned by someone else is reported and left alone.

Run `python -m data.ensure_indexes` first. The agent's stage timing is a static estimate
(`payment_agent/cutoff_evidence.py`), so there is no history to load. A database that still
has the retired `paymentStageEvents` collection can drop it: nothing reads it.
ABC (ACC-acc10001, CUST-abc10001) comes from `load_stage2_seed.py`.

DRY-RUN by default. Nothing writes without --apply.

    cd backend/transactions
    .venv/bin/python ../data/load_cutoff_seed.py --db fsi-bian-test-db            # preview
    .venv/bin/python ../data/load_cutoff_seed.py --db fsi-bian-test-db --apply    # load + verify
    .venv/bin/python ../data/load_cutoff_seed.py --db fsi-bian-test-db --verify   # check only
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Run from backend/transactions (venv + import root), per the sibling scripts.
sys.path.insert(0, str(Path.cwd()))
load_dotenv(".env")

from contexts.payment_orchestration.application import cutoff_seed as seed  # noqa: E402
from data.ensure_indexes import (  # noqa: E402
    CUTOFF_WORKFLOW_INDEXES,
    STAFF_DIRECTORY_INDEXES,
)
from shared import hold_queues  # noqa: E402

OWNED = {"sourceSystem": seed.SOURCE_SYSTEM}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _foreign(coll, key: str, ref: str) -> bool:
    """True when `ref` exists but belongs to another demo (never rewrite it)."""
    existing = coll.find_one({key: ref}, {"sourceSystem": 1})
    return existing is not None and existing.get("sourceSystem") != seed.SOURCE_SYSTEM


def load_owned(coll, key: str, docs: list[dict], apply: bool) -> None:
    """Upsert documents this seed owns; refresh them on every run (the story is dated)."""
    for doc in docs:
        ref = doc[key]
        if _foreign(coll, key, ref):
            log(f"  {ref:22s} owned by another source — leaving alone")
            continue
        if not apply:
            log(f"  {ref:22s} would UPSERT")
            continue
        coll.update_one({key: ref, **OWNED}, {"$set": doc}, upsert=True)
        log(f"  {ref:22s} UPSERTED")


def load_insert_only(coll, key: str, docs: list[dict], apply: bool) -> None:
    """Insert if absent; an existing document is never rewritten (its balance included, Q5)."""
    for doc in docs:
        ref = doc[key]
        if coll.find_one({key: ref}, {"_id": 1}):
            log(f"  {ref:22s} already present — leaving alone")
            continue
        if not apply:
            log(f"  {ref:22s} would INSERT")
            continue
        coll.update_one({key: ref}, {"$setOnInsert": doc}, upsert=True)
        log(f"  {ref:22s} INSERTED")


def join_abc_mandate(accounts, apply: bool) -> None:
    """Add Maya and Raj to ACC-acc10001's signatories. The `$ne` guard makes it idempotent
    on customerId, not on the whole subdocument."""
    if accounts.find_one({"accountId": seed.ABC_ACCOUNT}, {"_id": 1}) is None:
        log(f"  {seed.ABC_ACCOUNT} MISSING — run load_stage2_seed.py first")
        return
    for sig in seed.ABC_SIGNATORIES:
        flt = {"accountId": seed.ABC_ACCOUNT, "signatories.customerId": {"$ne": sig["customerId"]}}
        if accounts.find_one(flt, {"_id": 1}) is None:
            log(f"  {sig['customerId']} already a signatory on {seed.ABC_ACCOUNT}")
            continue
        if not apply:
            log(f"  {sig['customerId']} would JOIN {seed.ABC_ACCOUNT}")
            continue
        accounts.update_one(flt, {"$push": {"signatories": sig}})
        log(f"  {sig['customerId']} JOINED {seed.ABC_ACCOUNT}")


# --- verify -------------------------------------------------------------------

def _check(results: list, ok: bool, label: str) -> None:
    results.append(ok)
    log(f"  {'PASS' if ok else 'FAIL'}  {label}")


def verify(db, business_date) -> bool:
    log("Verification:")
    results: list = []
    staff = {s["staffId"]: s for s in db[hold_queues.STAFF_DIRECTORY].find(OWNED)}
    at_1816 = seed._et(business_date, "18:16")
    at_1750 = seed._et(business_date, "17:50")

    maya = staff.get(seed.STAFF_MAYA) or {}
    _check(results, bool(maya) and not hold_queues.on_shift(maya, at_1816)
           and (maya.get("outOfOfficeUntil") or datetime.min).replace(tzinfo=timezone.utc)
           >= seed.maya_back_at(business_date), "Maya out of office until next business day")
    _check(results, hold_queues.on_shift(staff.get(seed.STAFF_RAJ) or {}, at_1816),
           "Raj on shift at 18:16 ET")
    lena_open = db[hold_queues.WORKFLOW].count_documents(
        {"kind": hold_queues.APPROVAL_REQUEST, "assignedTo": seed.STAFF_LENA, "status": hold_queues.OPEN, "synthetic": True})
    _check(results, lena_open == 6, f"Lena has 6 OPEN requests (found {lena_open})")
    analysts = sum(1 for s in staff.values()
                   if s.get("role") == "ANALYST" and hold_queues.on_shift(s, at_1750))
    _check(results, analysts == 1, f"one analyst on shift at 17:50 ET (found {analysts})")

    abc = db["accounts"].find_one({"accountId": seed.ABC_ACCOUNT}) or {}
    signers = {s.get("customerId") for s in abc.get("signatories") or []}
    _check(results, {seed.MAYA, seed.RAJ} <= signers, "Maya and Raj sign on ACC-acc10001")

    short = db["accounts"].find_one({"accountId": seed.FUNDS_SHORT_ACCOUNT}) or {}
    available = (short.get("balance") or {}).get("available")
    credits = [c for c in short.get("expectedCredits") or [] if c.get("status") == "EXPECTED"]
    _check(results, available is not None and float(str(available)) <= 10_000 - 3_200
           and bool(credits),
           f"ACC-acc10003 available {available} with an expected credit")

    for name, specs in ((hold_queues.WORKFLOW, CUTOFF_WORKFLOW_INDEXES),
                        (hold_queues.STAFF_DIRECTORY, STAFF_DIRECTORY_INDEXES)):
        present = set(db[name].index_information())
        missing = [s["name"] for s in specs if s["name"] not in present]
        _check(results, not missing, f"{name} indexes{' missing ' + str(missing) if missing else ''}")

    return all(results)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load the cutoff demo seed (staff, approvers, funds-short account).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", required=True, help="target database (e.g. fsi-bian-test-db)")
    p.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    p.add_argument("--verify", action="store_true", help="verify only; no writes at all")
    args = p.parse_args()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        log("ERROR: MONGODB_URI is not set.")
        return 2

    business_date = seed.business_date_for(datetime.now(timezone.utc))
    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY-RUN")
    log(f"=== cutoff seed load [{mode}] · db={args.db} · business date {business_date} ===\n")

    client = MongoClient(uri)
    try:
        db = client[args.db]
        if not args.verify:
            log("staffDirectory:")
            load_owned(db[hold_queues.STAFF_DIRECTORY], "staffId",
                       seed.staff_docs(business_date), args.apply)
            log("accounts:")
            load_insert_only(db["accounts"], "accountId", [seed.funds_short_account_doc()],
                             args.apply)
            join_abc_mandate(db["accounts"], args.apply)
            log("cutoffWorkflow:")
            load_owned(db[hold_queues.WORKFLOW], "approvalRequestId",
                       seed.synthetic_lena_requests(business_date), args.apply)
            log("")
        if args.apply or args.verify:
            if not verify(db, business_date):
                log("\nVerification FAILED — see above.")
                return 1
    except PyMongoError as e:
        log(f"ERROR: {e}")
        return 1
    finally:
        client.close()

    log("\nDone." if args.apply or args.verify
        else "Done.  (dry-run — no writes; re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
