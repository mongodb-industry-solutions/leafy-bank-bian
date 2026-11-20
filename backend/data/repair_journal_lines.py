#!/usr/bin/env python3
"""Re-sum journal lines from the sub-ledger entries that still point at them.

Needed after `purge_w1demo_wires.py`: it deleted sub-ledger entries but kept the batch journal
that summarised them (a journal covers many payments), so the GL check
`reconciliation_service.reconcile_account` reports sl != jnl and the batch is skipped.

For every POSTED journal in the period, each line (accountCode, side) is recomputed as the sum of
its journal's POSTED sub-ledger entries. Lines that differ are rewritten (amount,
functionalAmount, description count) and `totalAmount` is recomputed; a line whose entries are all
gone is dropped, and a journal with no lines left is deleted. A journal that would end
unbalanced is reported and left untouched.

Dry-run by default; pass --apply to write.

    cd backend/transactions
    .venv/bin/python ../data/repair_journal_lines.py --period 2026-11
    .venv/bin/python ../data/repair_journal_lines.py --period 2026-11 --apply
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone

from bson import Int64
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")


def _actual_lines(sl, journal_id: str) -> dict:
    rows = sl.aggregate([
        {"$match": {"journalEntryId": journal_id, "status": "POSTED"}},
        {"$group": {"_id": {"code": "$controlAccountCode", "side": "$side"},
                    "amount": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ])
    return {(r["_id"]["code"], r["_id"]["side"]): (int(r["amount"]), r["count"]) for r in rows}


def _repair(journal: dict, actual: dict) -> tuple[list[dict], list[str]]:
    entries, changes = [], []
    for e in journal["entries"]:
        key = (e["accountCode"], e["side"])
        amount, count = actual.get(key, (0, 0))
        if amount == int(e["amount"]):
            entries.append(e)
            continue
        changes.append(f"{key[0]} {key[1]:6s} {int(e['amount']):>12d} -> {amount:>12d}")
        if amount == 0:
            continue
        fixed = dict(e, amount=Int64(amount), functionalAmount=Int64(amount))
        fixed["lineDescription"] = re.sub(r"^Sum of \d+", f"Sum of {count}", e.get("lineDescription", ""))
        entries.append(fixed)
    for n, e in enumerate(entries, start=1):
        e["lineNumber"] = n
    return entries, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--period", required=True, help="YYYY-MM, e.g. 2026-11")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
    print(f"database: {db.name}  period: {args.period}  mode: {'APPLY' if args.apply else 'dry-run'}")

    fixed = 0
    for journal in db["journalEntries"].find({"periodCode": args.period, "status": "POSTED"}):
        entries, changes = _repair(journal, _actual_lines(db["subLedgerEntries"], journal["journalId"]))
        if not changes:
            continue
        debit = sum(int(e["amount"]) for e in entries if e["side"] == "DEBIT")
        credit = sum(int(e["amount"]) for e in entries if e["side"] == "CREDIT")
        print(f"\n{journal['journalId']}")
        for c in changes:
            print(f"  {c}")
        if debit != credit:
            print(f"  SKIPPED: would be unbalanced (DR {debit} != CR {credit}); fix by hand")
            continue
        fixed += 1
        if not entries:
            # Every line's postings are gone: the journal summarised nothing that still exists.
            print("  all lines gone: journal will be deleted")
            if args.apply:
                db["journalEntries"].delete_one({"_id": journal["_id"]})
                print("  deleted")
            continue
        if args.apply:
            db["journalEntries"].update_one({"_id": journal["_id"]}, {"$set": {
                "entries": entries, "totalAmount": Int64(debit),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }})
            print("  rewritten")

    print(f"\n{fixed} journal(s) {'rewritten' if args.apply else 'to rewrite'}")
    if not args.apply and fixed:
        print("dry-run only; rerun with --apply to write")


if __name__ == "__main__":
    main()
