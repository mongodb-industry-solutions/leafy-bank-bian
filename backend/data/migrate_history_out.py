#!/usr/bin/env python3
"""Move `payments.lifecycle.events[]` and `payments.checks[]` into `paymentHistory`.

See `.claude/outputs/plan-lifecycle-history-out.md`. Run from `backend/transactions`:

    .venv/bin/python ../data/migrate_history_out.py              # dry-run (default)
    .venv/bin/python ../data/migrate_history_out.py --apply
    .venv/bin/python ../data/migrate_history_out.py --reverse --apply   # rollback

Order of a release: `python -m data.ensure_indexes`, stop writers, `--apply`, deploy, then
`verify_history_parity.py`.

Per payment, ONE transaction: insert the `EVENT`/`CHECK` documents, set `lifecycle.eventCount`
and `lifecycle.checkCount`, `$unset` the arrays. The transaction verifies the inserted count
before committing, so the arrays are only removed once their copies are in place. Idempotent: only
payments that still carry an array are selected, and history documents that already exist
(same deterministic `_id`) are not inserted twice.

`--apply` first copies `payments` to `payments_backup_<YYYYMMDD>` and refuses to touch any payment
if that copy is missing or smaller than the set being migrated.

`--reverse` rebuilds the arrays from `paymentHistory` and deletes the history documents; the
only rollback once new traffic has written history under the new build.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

HISTORY = "paymentHistory"
EVENT = "EVENT"
CHECK = "CHECK"
_ENVELOPE = ("_id", "paymentId", "rail", "kind", "seq")


def history_documents(payment: dict) -> list:
    """`EVENT` and `CHECK` documents for the arrays embedded on `payment`. Pure."""
    docs = []
    arrays = ((EVENT, (payment.get("lifecycle") or {}).get("events") or []),
              (CHECK, payment.get("checks") or []))
    for kind, entries in arrays:
        for seq, entry in enumerate(entries, start=1):
            docs.append({
                "_id": f"{payment['paymentId']}:{kind}:{seq}",
                "paymentId": payment["paymentId"],
                "rail": payment.get("rail"),
                "kind": kind,
                "seq": seq,
                **entry,
            })
    return docs


def embedded_arrays(history_docs: list) -> tuple:
    """The `(events, checks)` arrays rebuilt from history documents. Pure."""
    def pick(kind):
        docs = sorted((d for d in history_docs if d["kind"] == kind), key=lambda d: d["seq"])
        return [{k: v for k, v in d.items() if k not in _ENVELOPE} for d in docs]
    return pick(EVENT), pick(CHECK)


def _backup(db, apply: bool, needed: int) -> str:
    name = f"payments_backup_{datetime.now(timezone.utc):%Y%m%d}"
    if name in db.list_collection_names():
        have = db[name].count_documents({})
        if have < needed:
            raise SystemExit(f"{name} exists but holds {have} documents, fewer than the {needed} "
                             "payments to migrate; refusing to continue")
        print(f"backup {name} already present ({have} documents)")
        return name
    if apply:
        db.payments.aggregate([{"$match": {}}, {"$out": name}])
        print(f"backup written: {name} ({db[name].count_documents({})} documents)")
    else:
        print(f"[dry-run] would copy payments to {name}")
    return name


def migrate(db, apply: bool, batch: int) -> int:
    query = {"$or": [{"lifecycle.events": {"$exists": True}}, {"checks": {"$exists": True}}]}
    todo = db.payments.count_documents(query)
    print(f"{todo} payment(s) carry embedded events/checks [{'APPLY' if apply else 'DRY-RUN'}]")
    if not todo:
        return 0
    backup = _backup(db, apply, db.payments.count_documents({}))
    migrated = 0
    for payment in db.payments.find(query).batch_size(batch):
        docs = history_documents(payment)
        events = sum(1 for d in docs if d["kind"] == EVENT)
        if not apply:
            print(f"  {payment['paymentId']}: {events} event(s), {len(docs) - events} check(s)")
            continue
        if backup not in db.list_collection_names():
            raise SystemExit("backup collection missing; refusing to $unset")
        with db.client.start_session() as session:
            with session.start_transaction():
                have = {d["_id"] for d in db[HISTORY].find(
                    {"paymentId": payment["paymentId"]}, {"_id": 1}, session=session)}
                fresh = [d for d in docs if d["_id"] not in have]
                if fresh:
                    db[HISTORY].insert_many(fresh, session=session)
                stored = db[HISTORY].count_documents({"paymentId": payment["paymentId"]}, session=session)
                if stored != len(docs):
                    raise RuntimeError(f"{payment['paymentId']}: {stored} history docs, expected {len(docs)}")
                db.payments.update_one(
                    {"_id": payment["_id"]},
                    {"$set": {"lifecycle.eventCount": events, "lifecycle.checkCount": len(docs) - events},
                     "$unset": {"lifecycle.events": "", "checks": ""}},
                    session=session)
        migrated += 1
    print(f"migrated {migrated} payment(s)")
    return migrated


def reverse(db, apply: bool, batch: int) -> int:
    query = {"lifecycle.eventCount": {"$exists": True}}
    print(f"{db.payments.count_documents(query)} payment(s) use paymentHistory [{'APPLY' if apply else 'DRY-RUN'}]")
    reverted = 0
    for payment in db.payments.find(query, {"paymentId": 1}).batch_size(batch):
        if not apply:
            continue
        with db.client.start_session() as session:
            with session.start_transaction():
                docs = list(db[HISTORY].find({"paymentId": payment["paymentId"]}, session=session))
                events, checks = embedded_arrays(docs)
                db.payments.update_one(
                    {"_id": payment["_id"]},
                    {"$set": {"lifecycle.events": events, "checks": checks},
                     "$unset": {"lifecycle.eventCount": "", "lifecycle.checkCount": ""}},
                    session=session)
                db[HISTORY].delete_many({"paymentId": payment["paymentId"]}, session=session)
        reverted += 1
    print(f"reverted {reverted} payment(s)")
    return reverted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--reverse", action="store_true", help="rebuild the arrays from paymentHistory")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()
    load_dotenv(".env")
    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
    (reverse if args.reverse else migrate)(db, args.apply, args.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
