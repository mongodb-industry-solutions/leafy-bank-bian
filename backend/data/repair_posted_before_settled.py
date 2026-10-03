#!/usr/bin/env python3
"""Repair outbound wires the ledger moved to POSTED before they settled (2026-09-30).

A GL batch inside the ~30s deferred-settlement window moved a wire IN_PROGRESS -> POSTED
while `lifecycle.settlementStatus` was still PENDING. `settle.complete_due` only selects
IN_PROGRESS, so those wires never settled. The write-back is fixed
(`posting_writeback_service.py`); this puts the already-stuck ones back at IN_PROGRESS so
`complete_due` settles them on its next poll. `postingStatus` stays POSTED — the posting
fact is true.

    cd backend/transactions
    .venv/bin/python ../data/repair_posted_before_settled.py            # dry-run
    .venv/bin/python ../data/repair_posted_before_settled.py --apply
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
apply = "--apply" in sys.argv

stuck = list(db.payments.find(
    {"lifecycle.currentState": "POSTED", "lifecycle.settlementStatus": "PENDING"},
    {"paymentId": 1, "simulatedSettlementOutcome": 1},
))
print(f"{len(stuck)} wire(s) POSTED with settlement PENDING [{'APPLY' if apply else 'DRY-RUN'}]")
for p in stuck:
    print(" ", p["paymentId"], p.get("simulatedSettlementOutcome"))
    if not apply:
        continue
    now = datetime.now(timezone.utc)
    db.payments.update_one(
        {"_id": p["_id"], "lifecycle.currentState": "POSTED"},
        {"$set": {"lifecycle.currentState": "IN_PROGRESS", "lifecycle.stateEnteredAt": now,
                  "status": "IN_PROGRESS", "updatedAt": now},
         "$push": {"lifecycle.events": {
             "state": "IN_PROGRESS", "at": now, "actor": "repair-script", "actorType": "SERVICE",
             "reason": "Repair: POSTED before settlement confirmed; returned to await settlement",
         }}},
    )
if stuck and not apply:
    print("re-run with --apply")
