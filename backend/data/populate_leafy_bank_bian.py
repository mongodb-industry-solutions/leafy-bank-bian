#!/usr/bin/env python3
"""
Core-banking recreate + populate for leafy_bank_bian.

Replays the mongodump snapshot at dump-core/leafy_bank_bian/ (9 collections:
customers, accounts, glAccounts, payments, transactions, notifications,
ledgerEvents, subLedgerEntries, journalEntries) into a target database, then
rebuilds indexes + validators by calling the repo's OWN canonical setup code —
backend/ledger/data/ensure_indexes.py and backend/transactions/data/ensure_indexes.py.

Why call those instead of the dump's metadata.json: those two modules are the
spec-aligned single source of truth (idx names, unique/partial flags, the
ledgerEvents/subLedgerEntries/journalEntries JSON-Schema + Pacioli balance
validators). Copying their definitions here would just create another mirror to
drift (this repo has logged that class of bug repeatedly). So this script imports
and runs them — no duplication.

  customers / accounts / notifications  -> only the automatic _id index (nothing to ensure)
  glAccounts / ledgerEvents / subLedgerEntries / journalEntries / changeStreamTokens
                                        -> ensure_ledger_indexes()  (+ 3 validators)
  payments / transactions               -> ensure_transactions_indexes()  (idx_payment_id)

NOT in the dump by design (so not loaded here): the chart-summary view (recreate
via mongosh — pipeline is in the ist-shared snapshot) and changeStreamTokens
(workers must start fresh from "now"; ensure_ledger_indexes creates it empty).

Load-time rename override (backward-compatible, no-op when unset): set
COLLECTION_RENAME_OVERRIDES to a JSON {defaultCollectionName: newName} to land the
DATA under different names. NOTE: the index/validator step calls the canonical
ensure_* code, which uses the canonical names — so renaming a core collection and
also wanting its indexes means running ensure separately against the new name.
Core-banking collections are all in the "no rename" set (collection-rename-proposal.md),
so this is not expected in practice; a warning is printed if an override touches one.

DRY-RUN by default. Nothing writes without --apply.

Usage (MONGODB_URI in env — same convention as the other populate scripts):
    export MONGODB_URI="mongodb+srv://..."
    python populate_leafy_bank_bian.py                              # dry-run preview
    python populate_leafy_bank_bian.py --apply --db fin_migration   # stage
    python populate_leafy_bank_bian.py --apply --db leafy_bank_bian # promote
    python populate_leafy_bank_bian.py --apply --drop-existing      # wipe targets first
    python populate_leafy_bank_bian.py --apply --collections payments transactions
    python populate_leafy_bank_bian.py --apply --skip-indexes       # data only
    python populate_leafy_bank_bian.py --skip-data --apply          # indexes/validators only

Idempotent: data copied via _id-keyed upsert; ensure_* index/validator calls are
idempotent (create_index / collMod no-op when already matching).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

from bson import decode_file_iter
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, PyMongoError

# repos/leaf-bank-bian/backend/data/ -> leafy-bank-setup/
ROOT = Path(__file__).resolve().parents[4]
REPO = Path(__file__).resolve().parents[2]                 # repos/leaf-bank-bian
# Canonical dump home lives beside the db-setup tooling. Override with --dump-dir.
DEFAULT_DUMP_DIR = ROOT / "bian-data-model" / "migration" / "db-setup" / "dump-core" / "leafy_bank_bian"

# Collections in load order. Parents before children isn't required (no FK
# enforcement in Mongo), but this order reads naturally and matches the dump.
COLLECTIONS = [
    "customers", "accounts", "glAccounts",
    "payments", "transactions", "notifications",
    "ledgerEvents", "subLedgerEntries", "journalEntries",
]
BATCH = 1000


# ---------------------------------------------------------------------------
# Load-time collection-rename override (backward-compatible; no-op when unset).
# ---------------------------------------------------------------------------
def _load_rename_overrides():
    path = os.getenv("COLLECTION_RENAME_OVERRIDES")
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


_RENAME_OVERRIDES = _load_rename_overrides()


def _ovr(name):
    return _RENAME_OVERRIDES.get(name, name)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Data load (BSON -> target), idempotent _id upsert
# ---------------------------------------------------------------------------
def load_collection(db, dump_dir: Path, name: str, apply: bool, drop_existing: bool) -> tuple[int, int]:
    bson_path = dump_dir / f"{name}.bson"
    target = _ovr(name)
    if not bson_path.exists():
        log(f"  {name:16s} -> {target:16s}  ! {bson_path.name} not found — skipping")
        return 0, 0

    docs = list(decode_file_iter(bson_path.open("rb")))
    src_count = len(docs)
    log(f"  {name:16s} -> {target:16s}  {src_count:>6d} docs")

    if not apply:
        return src_count, 0

    coll = db[target]
    if drop_existing:
        coll.drop()
        log(f"    dropped existing {target}")

    written = 0
    ops = []
    for doc in docs:
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
        if len(ops) >= BATCH:
            written += _flush(coll, ops)
            ops = []
    if ops:
        written += _flush(coll, ops)

    dst_count = coll.count_documents({})
    flag = "OK" if dst_count == src_count else "MISMATCH"
    log(f"    wrote {written}; target now {dst_count} [{flag}]")
    return src_count, written


def _flush(coll, ops) -> int:
    try:
        res = coll.bulk_write(ops, ordered=False)
        return res.upserted_count + res.modified_count
    except BulkWriteError as e:
        log(f"    ! bulk write errors: {e.details.get('writeErrors', [])[:3]}")
        return 0


# ---------------------------------------------------------------------------
# Index + validator setup — import and call the repo's canonical ensure_* code
# ---------------------------------------------------------------------------
def _import_ensure(service: str):
    """Load backend/<service>/data/ensure_indexes.py as a uniquely-named module.

    Its `from database.connection import MongoDBConnection` is an absolute import,
    so the service dir must be on sys.path. `from dotenv import load_dotenv` runs at
    import time — shim it if python-dotenv isn't installed (we don't use .env here;
    URI/db come from CLI/env)."""
    if importlib.util.find_spec("dotenv") is None:
        shim = types.ModuleType("dotenv")
        shim.load_dotenv = lambda *a, **k: None
        sys.modules["dotenv"] = shim

    svc_dir = REPO / "backend" / service
    if str(svc_dir) not in sys.path:
        sys.path.insert(0, str(svc_dir))

    path = svc_dir / "data" / "ensure_indexes.py"
    spec = importlib.util.spec_from_file_location(f"{service}_ensure_indexes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ensure_indexes_and_validators(uri: str, db_name: str, apply: bool) -> None:
    ledger = _import_ensure("ledger")
    txn = _import_ensure("transactions")
    # Both service dirs ship an identical database/connection.py; reuse whichever
    # imported first so the connection type matches what the ensure_* fns expect.
    Connection = ledger.MongoDBConnection

    if not apply:
        log("  [dry-run] would call ensure_ledger_indexes() "
            "(glAccounts/ledgerEvents/subLedgerEntries/journalEntries/changeStreamTokens + 3 validators)")
        log("  [dry-run] would call ensure_transactions_indexes() (payments/transactions idx_payment_id)")
        return

    conn = Connection(uri)
    led = ledger.ensure_ledger_indexes(conn, db_name)
    for coll, names in led.items():
        log(f"  ledger  {coll}: {names}")
    tx = txn.ensure_transactions_indexes(conn, db_name)
    for coll, names in tx.items():
        log(f"  txn     {coll}: {names}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Recreate + populate core-banking collections in leafy_bank_bian from the mongodump.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", default="leafy_bank_bian", help="target database (default: leafy_bank_bian)")
    p.add_argument("--dump-dir", default=str(DEFAULT_DUMP_DIR),
                   help=f"mongodump folder (default: {DEFAULT_DUMP_DIR})")
    p.add_argument("--collections", nargs="*", default=None,
                   help=f"subset to load (default: all). Choices: {COLLECTIONS}")
    p.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    p.add_argument("--drop-existing", action="store_true", help="drop each target collection before loading")
    p.add_argument("--skip-data", action="store_true", help="skip data load (indexes/validators only)")
    p.add_argument("--skip-indexes", action="store_true", help="skip index/validator setup (data only)")
    args = p.parse_args()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        log("ERROR: MONGODB_URI is not set.")
        return 2

    dump_dir = Path(args.dump_dir)
    selected = args.collections or COLLECTIONS
    unknown = [c for c in selected if c not in COLLECTIONS]
    if unknown:
        log(f"ERROR: unknown collection(s): {unknown}. Known: {COLLECTIONS}")
        return 2

    if _RENAME_OVERRIDES:
        touched = sorted(set(selected) & set(_RENAME_OVERRIDES))
        if touched:
            log(f"  WARNING: rename override touches core collection(s) {touched}; DATA lands on the new "
                f"name(s), but the index/validator step uses canonical names. Run ensure_* separately for those.\n")

    mode = "APPLY" if args.apply else "DRY-RUN"
    log(f"=== core-banking populate [{mode}] · db={args.db} · dump={dump_dir} ===\n")

    client = MongoClient(uri)
    try:
        if not args.skip_data:
            log("Data load:")
            totals = [load_collection(client[args.db], dump_dir, c, args.apply, args.drop_existing)
                      for c in selected]
            n = sum(s for s, _ in totals)
            log(f"\n  {len(totals)} collections, {n} source docs\n")

        if not args.skip_indexes:
            log("Indexes + validators (canonical ensure_* code):")
            ensure_indexes_and_validators(uri, args.db, args.apply)
    except PyMongoError as e:
        log(f"ERROR: {e}")
        return 1
    finally:
        client.close()

    log("\nDone." + ("" if args.apply else "  (dry-run — no writes; re-run with --apply)"))
    log("Reminder: recreate chart-summary-view separately (mongosh; pipeline in the ist-shared snapshot).")
    return 0


if __name__ == "__main__":
    sys.exit(main())