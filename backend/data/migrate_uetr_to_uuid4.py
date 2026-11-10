#!/usr/bin/env python3
"""Rewrite every stored UETR to the ISO 20022 / SWIFT gpi format (2026-11-10).

A UETR is a lowercase UUIDv4: xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx. Older payments
carried `UETR-<ObjectId>`, `UETR-<uuid4>` (simulated inbound), or an ObjectId padded to UUID
shape (seed data). The UETR is copied into ISO messages and the inbound idempotency key, so
one old->new mapping is applied to every copy to keep them joined:

  payments.uetr, payments.idempotency.idempotencyKey (`INBOUND-<uetr>`),
  paymentExecutions + canonicalJsonStorage `uetr` / `UETR` / `OrgnlUETR` at any depth.

A value whose core (prefix stripped) is already a valid UUIDv4 keeps that core; anything else
gets a fresh uuid4. Backup collections are left untouched.

    cd backend/transactions
    .venv/bin/python ../data/migrate_uetr_to_uuid4.py            # dry-run
    .venv/bin/python ../data/migrate_uetr_to_uuid4.py --apply
"""

import os
import re
import sys
import uuid

from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne

UETR_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
UETR_KEYS = {"uetr", "UETR", "OrgnlUETR"}
INBOUND_PREFIX = "INBOUND-"
COLLECTIONS = ("payments", "paymentExecutions", "canonicalJsonStorage")

load_dotenv(".env")
db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")]
apply = "--apply" in sys.argv

_mapping: dict[str, str] = {}


def fixed(value: str) -> str:
    core = value.removeprefix("UETR-").lower()
    if UETR_RE.match(core):
        return core
    # Memoised on the core so the payment and its prefix-stripped ISO copies map identically.
    return _mapping.setdefault(core, str(uuid.uuid4()))


def rewrite(node):
    """Return (new_node, changed) with every UETR-bearing string corrected."""
    changed = False
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in UETR_KEYS and isinstance(value, str) and not UETR_RE.match(value):
                out[key], changed = fixed(value), True
            # Only UETR-derived keys; the `INBOUND-<msgId>-<txId>` fallback must stay as-is.
            elif key == "idempotencyKey" and isinstance(value, str) and value.startswith(INBOUND_PREFIX + "UETR-"):
                out[key], changed = INBOUND_PREFIX + fixed(value[len(INBOUND_PREFIX):]), True
            else:
                out[key], sub = rewrite(value)
                changed = changed or sub
        return out, changed
    if isinstance(node, list):
        items = [rewrite(v) for v in node]
        return [v for v, _ in items], any(c for _, c in items)
    return node, False


print(f"db={db.name} [{'APPLY' if apply else 'DRY-RUN'}]")
for name in COLLECTIONS:
    ops = []
    for doc in db[name].find({}):
        new, changed = rewrite(doc)
        if changed:
            ops.append(ReplaceOne({"_id": doc["_id"]}, new))
    print(f"  {name}: {len(ops)} document(s) to fix")
    if apply and ops:
        result = db[name].bulk_write(ops, ordered=False)
        print(f"    modified {result.modified_count}")

if not apply:
    print("re-run with --apply")
