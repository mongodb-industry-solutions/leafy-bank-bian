#!/usr/bin/env python3
"""Parity gate for the lifecycle-history-out migration. Read-only; exit 1 on any failure.

    cd backend/transactions
    .venv/bin/python ../data/verify_history_parity.py

Checks, per payment (success criteria 1 and 2 of the plan):
  * `lifecycle.eventCount` equals the number of `EVENT` documents, with seq exactly 1..N
  * `lifecycle.checkCount` equals the number of `CHECK` documents, with seq exactly 1..N
  * the payment has no embedded `lifecycle.events` or `checks`
  * `status == lifecycle.currentState`, and the latest event's state equals `currentState`
"""

import os
import sys
from collections import defaultdict

from dotenv import load_dotenv
from pymongo import MongoClient


def check_payment(payment: dict, seqs: dict) -> list:
    """Problems for one payment. `seqs` maps kind -> sorted seq list. Pure."""
    problems = []
    lc = payment.get("lifecycle") or {}
    for kind, counter in (("EVENT", "eventCount"), ("CHECK", "checkCount")):
        found = seqs.get(kind, [])
        if lc.get(counter) != len(found):
            problems.append(f"{counter}={lc.get(counter)} but {len(found)} {kind} documents")
        if found != list(range(1, len(found) + 1)):
            problems.append(f"{kind} seq is not 1..{len(found)}: {found[:5]}...")
    if "events" in lc:
        problems.append("embedded lifecycle.events still present")
    if "checks" in payment:
        problems.append("embedded checks still present")
    if payment.get("status") != lc.get("currentState"):
        problems.append(f"status {payment.get('status')} != currentState {lc.get('currentState')}")
    return problems


def main() -> int:
    load_dotenv(".env")
    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
    seqs = defaultdict(lambda: defaultdict(list))
    last_state = {}
    for h in db.paymentHistory.find({}, {"paymentId": 1, "kind": 1, "seq": 1, "state": 1}).sort("seq", 1):
        seqs[h["paymentId"]][h["kind"]].append(h["seq"])
        if h["kind"] == "EVENT":
            last_state[h["paymentId"]] = h.get("state")
    failed = 0
    total = 0
    legacy = 0
    for p in db.payments.find({}, {"paymentId": 1, "status": 1, "lifecycle": 1, "checks": 1}):
        # Pre-lifecycle documents never had events or checks, so there is nothing to compare.
        if "lifecycle" not in p and "checks" not in p and p["paymentId"] not in seqs:
            legacy += 1
            continue
        total += 1
        problems = check_payment(p, seqs.get(p["paymentId"], {}))
        current = (p.get("lifecycle") or {}).get("currentState")
        if last_state.get(p["paymentId"]) != current:
            problems.append(f"latest event {last_state.get(p['paymentId'])} != currentState {current}")
        if problems:
            failed += 1
            print(f"FAIL {p['paymentId']}: " + "; ".join(problems))
    orphans = set(seqs) - {p["paymentId"] for p in db.payments.find({}, {"paymentId": 1})}
    for pid in sorted(orphans):
        failed += 1
        print(f"FAIL {pid}: history documents with no payment")
    print(f"{'PASS' if not failed else 'FAIL'}: {total} payment(s), {failed} failing "
          f"({legacy} pre-lifecycle payment(s) skipped)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
