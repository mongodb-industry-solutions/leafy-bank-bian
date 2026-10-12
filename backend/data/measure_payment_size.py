#!/usr/bin/env python3
"""P0 of the lifecycle-history-out plan: how big are `payments` documents, and how much of that
is `lifecycle.events[]` + `checks[]`? Read-only.

    cd backend/transactions
    .venv/bin/python ../data/measure_payment_size.py

Run it before the migration (history embedded) and after (counters only) and record both in the
decision log. Sizes are `$bsonSize` in bytes.
"""

import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]

rows = list(db.payments.aggregate([
    {"$project": {
        "total": {"$bsonSize": "$$ROOT"},
        "lifecycle": {"$bsonSize": {"$ifNull": ["$lifecycle", {}]}},
        "checks": {"$bsonSize": {"$ifNull": [{"$arrayToObject": [[{"k": "c", "v": {"$ifNull": ["$checks", []]}}]]}, {}]}},
    }},
]))


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, round(p * (len(values) - 1)))]


if not rows:
    raise SystemExit("no payments")
for field in ("total", "lifecycle", "checks"):
    v = [r[field] for r in rows]
    print(f"{field:10s} n={len(v)} p50={pct(v, .5)} p90={pct(v, .9)} p99={pct(v, .99)} max={max(v)}")
hist = sum(r["lifecycle"] + r["checks"] for r in rows)
print(f"lifecycle+checks share of total bytes: {hist / sum(r['total'] for r in rows):.1%}")
