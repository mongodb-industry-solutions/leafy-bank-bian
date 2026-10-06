#!/usr/bin/env python3
"""Load the cutoff demo's seeded world (cutoff plan Part B) into a target database.

Writes what `cutoff_seed` builds — nothing else:

  staffDirectory      Maya (OOO), Raj, Lena, analysts 1-3          upsert by staffId
  accounts            ACC-acc10003 (6,300 + an expected credit)     $setOnInsert by accountId
                      Maya + Raj joined to ACC-acc10001's mandate  pushed only if absent
  approvalRequests    Lena's six synthetic OPEN requests           upsert by approvalRequestId
  paymentStageEvents  ~14 days of SEED history (time series)       replaced once per day

The database is shared with other demos, so the loader updates only documents it owns
(`sourceSystem: cutoff-demo-seed`): an existing account is never rewritten, and
a staff id or request id already owned by someone else is reported and left alone. History
is replaced by `source: "SEED"` (needs MongoDB >= 7.0 to delete on a non-meta field, R3).

Run `python -m data.ensure_indexes` first: it creates `paymentStageEvents` as a time series.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import quantiles

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Run from backend/transactions (venv + import root), per the sibling scripts.
sys.path.insert(0, str(Path.cwd()))
load_dotenv(".env")

from contexts.payment_orchestration.application import cutoff_seed as seed  # noqa: E402
from data.ensure_indexes import (  # noqa: E402
    APPROVAL_REQUESTS_INDEXES,
    SCREENING_QUEUE_INDEXES,
    STAFF_DIRECTORY_INDEXES,
    STAGE_EVENTS_COLLECTION,
)
from shared import business_clock, hold_queues  # noqa: E402

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


def load_history(db, business_date, apply: bool) -> None:
    events = db[STAGE_EVENTS_COLLECTION]
    batch = business_date.isoformat()
    if events.find_one({"source": seed.SEED_SOURCE, "seedBatch": batch}, {"_id": 1}):
        log(f"  seedBatch {batch} already loaded — unchanged")
        return
    existing = list(db.list_collections(filter={"name": STAGE_EVENTS_COLLECTION}))
    if not existing or existing[0].get("type") != "timeseries":
        log(f"  {STAGE_EVENTS_COLLECTION} is not a time series — run "
            "`python -m data.ensure_indexes` first")
        return
    points = seed.build_history(end_date=business_date)
    old = events.count_documents({"source": seed.SEED_SOURCE})
    log(f"  would replace {old} SEED point(s) with {len(points)} (seedBatch {batch})")
    if not apply:
        return
    events.delete_many({"source": seed.SEED_SOURCE})
    events.insert_many(points, ordered=False)
    log(f"  inserted {len(points)} SEED point(s)")


# --- verify -------------------------------------------------------------------

def _check(results: list, ok: bool, label: str) -> None:
    results.append(ok)
    log(f"  {'PASS' if ok else 'FAIL'}  {label}")


def _stage_percentiles(db) -> None:
    per = {}
    for p in db[STAGE_EVENTS_COLLECTION].find({"source": seed.SEED_SOURCE},
                                              {"at": 1, "durationMs": 1, "meta.stage": 1}):
        entered = p["at"].replace(tzinfo=timezone.utc) - timedelta(milliseconds=p["durationMs"])
        late = business_clock.minutes_since_midnight_et(entered) >= 17 * 60
        per.setdefault((p["meta"]["stage"], late), []).append(p["durationMs"] / 60000)
    log("  stage p50/p90 minutes (before 17:00 | after):")
    for stage in sorted({s for s, _ in per}):
        cells = []
        for late in (False, True):
            values = per.get((stage, late), [])
            if len(values) >= 2:
                q = quantiles(values, n=10)
                cells.append(f"{q[4]:6.1f} {q[8]:6.1f} (n={len(values)})")
            else:
                cells.append(f"{'—':>20s}")
        log(f"    {stage:18s} {cells[0]:24s} | {cells[1]}")


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
    lena_open = db[hold_queues.APPROVAL_REQUESTS].count_documents(
        {"assignedTo": seed.STAFF_LENA, "status": hold_queues.OPEN, "synthetic": True})
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

    info = list(db.list_collections(filter={"name": STAGE_EVENTS_COLLECTION}))
    _check(results, bool(info) and info[0].get("type") == "timeseries",
           f"{STAGE_EVENTS_COLLECTION} is a time series")
    points = db[STAGE_EVENTS_COLLECTION].count_documents({"source": seed.SEED_SOURCE})
    _check(results, 3_000 <= points <= 5_000, f"3-5k SEED points (found {points})")
    if points:
        _stage_percentiles(db)

    for name, specs in ((hold_queues.APPROVAL_REQUESTS, APPROVAL_REQUESTS_INDEXES),
                        (hold_queues.SCREENING_QUEUE, SCREENING_QUEUE_INDEXES),
                        (hold_queues.STAFF_DIRECTORY, STAFF_DIRECTORY_INDEXES)):
        present = set(db[name].index_information())
        missing = [s["name"] for s in specs if s["name"] not in present]
        _check(results, not missing, f"{name} indexes{' missing ' + str(missing) if missing else ''}")

    return all(results)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load the cutoff demo seed (staff, approvers, funds-short account, history).",
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
            log("approvalRequests:")
            load_owned(db[hold_queues.APPROVAL_REQUESTS], "approvalRequestId",
                       seed.synthetic_lena_requests(business_date), args.apply)
            log(f"{STAGE_EVENTS_COLLECTION}:")
            load_history(db, business_date, args.apply)
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
