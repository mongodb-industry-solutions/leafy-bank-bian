#!/usr/bin/env python3
"""Backfill ~14 days of correspondent-statement history for the reconciliation agent (plan B2).

Writes the off-chain history `statement_history.build_history` produces: daily camt.053
statements (`statement.historical: true`) on the USD nostro, plus the resolved exceptions
their non-clean lines would have raised (`historical{}` set). Live matching, orphan-raising
and live statement generation all ignore it — see that module's docstring.

Idempotent by replacement: every run deletes the previous history (only docs carrying the
flag, never a live one) and writes the new set, so a re-run with the same --seed and day
converges on the same evidence. Dry-run by default.

    cd backend/transactions
    .venv/bin/python ../data/generate_statements.py                     # preview
    .venv/bin/python ../data/generate_statements.py --apply             # replace + verify
    .venv/bin/python ../data/generate_statements.py --verify            # report only
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles

from dotenv import load_dotenv
from pymongo import MongoClient

# Run from backend/transactions (venv + import root), per the sibling scripts.
sys.path.insert(0, str(Path.cwd()))
load_dotenv(".env")

from contexts.financial_gateway.application.statement_history import build_history  # noqa: E402

_STATEMENTS = {"purpose": "ACCOUNT_STATEMENT", "statement.historical": True}
_EXCEPTIONS = {"historical": {"$exists": True}}


def report(db) -> None:
    stmts = list(db.paymentMessages.find(_STATEMENTS, {"entries": 1, "statement": 1}))
    print(f"statements: {len(stmts)} historical")
    lines = [e for s in stmts for e in s.get("entries", [])]
    print("  lines by outcome:", dict(Counter(e.get("simulatedOutcome") for e in lines)))
    lags = sorted((s["statement"]["window"]["to"] - e["settledAt"]).total_seconds() / 3600
                  for s in stmts for e in s["entries"] if e.get("settledAt"))
    if len(lags) >= 2:
        q = quantiles(lags, n=10)
        print(f"  lag hours: p50 {q[4]:.1f}  p90 {q[8]:.1f}  (n={len(lags)})")

    excs = list(db.exceptions.find(_EXCEPTIONS, {"category": 1, "historical": 1, "resolution.action": 1}))
    print(f"exceptions: {len(excs)} historical precedents")
    per = Counter((x["historical"]["correspondentBic"], x["category"], x["resolution"]["action"])
                  for x in excs)
    for (bic, cat, action), n in sorted(per.items()):
        print(f"  {bic:9s} {cat:27s} {action:22s} {n}")
    live_open = db.exceptions.count_documents({"status": "OPEN", **_EXCEPTIONS})
    if live_open:
        print(f"  ERROR: {live_open} historical exception(s) are OPEN")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--backfill-days", type=int, default=14)
    p.add_argument("--seed", type=int, default=1039)
    args = p.parse_args()

    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
    db = MongoClient(os.environ["MONGODB_URI"])[db_name]
    print(f"=== statement history [{'VERIFY' if args.verify else 'APPLY' if args.apply else 'DRY-RUN'}]"
          f" · db={db_name} ===")
    if args.verify:
        report(db)
        return 0

    statements, exceptions = build_history(now=datetime.now(timezone.utc),
                                           days=args.backfill_days, seed=args.seed)
    old_s = db.paymentMessages.count_documents(_STATEMENTS)
    old_e = db.exceptions.count_documents(_EXCEPTIONS)
    print(f"would replace {old_s} statement(s) + {old_e} exception(s) "
          f"with {len(statements)} + {len(exceptions)}")
    if not args.apply:
        print("dry-run — re-run with --apply")
        return 0

    db.paymentMessages.delete_many(_STATEMENTS)
    db.exceptions.delete_many(_EXCEPTIONS)
    db.paymentMessages.insert_many(statements)
    db.exceptions.insert_many(exceptions)
    report(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
