"""Idempotent ledger index creator.

Run: ``python -m data.ensure_indexes`` (or ``make ensure-indexes``).

The chart of accounts is already populated in ``leafy_bank_bian.glAccounts``; this script
does **not** seed data. ``create_index`` is idempotent — re-running is a no-op when the
index already matches.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from pymongo import ASCENDING

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

# Kept byte-for-byte aligned with the v4_30 spec.
# (verify: `python3 bian-data-model/bian.py indexes <coll>`)
GL_ACCOUNTS_INDEXES = [
    {"name": "idx_account_code_unique", "keys": [("accountCode", ASCENDING)], "unique": True},
    {"name": "idx_account_type", "keys": [("accountType", ASCENDING)]},
    {"name": "idx_subledger_type", "keys": [("subLedgerType", ASCENDING)], "sparse": True},
    {"name": "idx_parent_account", "keys": [("parentAccountCode", ASCENDING)], "sparse": True},
]

LEDGER_EVENTS_INDEXES = [
    {"name": "idx_event_id_unique", "keys": [("eventId", ASCENDING)], "unique": True},
    {"name": "idx_idempotency_key_unique", "keys": [("idempotencyKey", ASCENDING)], "unique": True},
    {"name": "idx_group_id", "keys": [("groupId", ASCENDING)]},
    {"name": "idx_posting_status_occurred", "keys": [("postingStatus", ASCENDING), ("occurredAt", ASCENDING)]},
]

SUBLEDGER_ENTRIES_INDEXES = [
    {"name": "idx_subledger_id_unique", "keys": [("subLedgerId", ASCENDING)], "unique": True},
    {"name": "idx_idempotency_key_unique", "keys": [("idempotencyKey", ASCENDING)], "unique": True},
    # Partial index: only covers journaled rows (journalEntryId != ""); excludes the ""
    # sentinel so the index stays compact. sparse=True would not exclude "" strings.
    {"name": "idx_journal_entry_id", "keys": [("journalEntryId", ASCENDING)],
     "partialFilterExpression": {"journalEntryId": {"$gt": ""}}},
    # Covers run_batch's {status, journalEntryId} match + {periodCode, controlAccountCode} grouping.
    {"name": "idx_batch_sweep", "keys": [
        ("status", ASCENDING), ("journalEntryId", ASCENDING),
        ("periodCode", ASCENDING), ("controlAccountCode", ASCENDING),
    ]},
    {"name": "idx_control_account_status", "keys": [("controlAccountCode", ASCENDING), ("status", ASCENDING)]},
    # Reconciliation period-scoped query: controlAccountCode + status=POSTED + periodCode.
    # Superset of idx_control_account_status; also serves the all-time path via prefix scan.
    {"name": "idx_control_account_status_period", "keys": [("controlAccountCode", ASCENDING), ("status", ASCENDING), ("periodCode", ASCENDING)]},
]

JOURNAL_ENTRIES_INDEXES = [
    {"name": "idx_journal_id_unique", "keys": [("journalId", ASCENDING)], "unique": True},
    {"name": "idx_idempotency_key_unique", "keys": [("idempotencyKey", ASCENDING)], "unique": True},
    {"name": "idx_period_code", "keys": [("periodCode", ASCENDING)]},
    # Multikey index for reconciliation: Σ entries[].amount WHERE entries.accountCode = X.
    # Period-scoped queries hit idx_period_code first (document level), then this index on
    # the unwound entries — keeps the reconciliation aggregation bounded to monthly volume.
    {"name": "idx_entries_account_code", "keys": [("entries.accountCode", ASCENDING)]},
]

STREAM_TOKENS_INDEXES = [
    {"name": "idx_worker_id_unique", "keys": [("workerId", ASCENDING)], "unique": True},
]

_LEG_SCHEMA = {
    "bsonType": "object",
    "required": ["glAccountCode", "controlAccountCode", "amount", "currency", "entityReference"],
    "properties": {
        "glAccountCode": {"bsonType": "string"},
        "controlAccountCode": {"bsonType": "string"},
        "amount": {"bsonType": ["long", "int"]},
        "currency": {"bsonType": "string"},
        "entityReference": {
            "bsonType": "object",
            "required": ["entityType", "entityId"],
            "properties": {
                "entityType": {"bsonType": "string"},
                "entityId": {"bsonType": "string"},
            },
        },
    },
}

_SOURCE_REF_SCHEMA = {
    "bsonType": "object",
    "required": ["sourceCollection", "sourceId", "sourceSystem"],
    "properties": {
        "sourceCollection": {"bsonType": "string"},
        "sourceId": {"bsonType": "string"},
        "sourceSystem": {"bsonType": "string"},
    },
}

# Enforces required business fields on ledgerEvents and rejects writes with
# invalid postingStatus values. postingResult is intentionally unrestricted
# (written in two stages: subledger IDs at Step 2, journalEntryId at Step 3).
_LEDGER_EVENTS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "eventId", "idempotencyKey", "groupId",
            "occurredAt", "valueDate",
            "eventType", "debitLeg", "creditLeg",
            "postingStatus", "sourceReference", "mappingVersion",
        ],
        "properties": {
            "eventId": {"bsonType": "string"},
            "idempotencyKey": {"bsonType": "string"},
            "groupId": {"bsonType": "string"},
            "occurredAt": {"bsonType": "date"},
            "valueDate": {"bsonType": "date"},
            "eventType": {"bsonType": "string"},
            "postingStatus": {"bsonType": "string", "enum": ["PENDING", "POSTED", "FAILED"]},
            "mappingVersion": {"bsonType": "string"},
            "debitLeg": _LEG_SCHEMA,
            "creditLeg": _LEG_SCHEMA,
            "sourceReference": _SOURCE_REF_SCHEMA,
        },
    }
}

# Enforces required business fields on subLedgerEntries and locks side/status
# to their allowed enum values. journalEntryId is required (the "" sentinel
# fulfills this until gl_batch stamps the real ID).
_SUBLEDGER_ENTRIES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "subLedgerId", "idempotencyKey",
            "controlAccountCode", "side", "amount", "currency",
            "periodCode", "status", "sourceReference",
            "entityReference", "journalEntryId",
        ],
        "properties": {
            "subLedgerId": {"bsonType": "string"},
            "idempotencyKey": {"bsonType": "string"},
            "controlAccountCode": {"bsonType": "string"},
            "side": {"bsonType": "string", "enum": ["DEBIT", "CREDIT"]},
            "amount": {"bsonType": ["long", "int"]},
            "currency": {"bsonType": "string"},
            "periodCode": {"bsonType": "string"},
            "status": {"bsonType": "string", "enum": ["POSTED", "FAILED"]},
            "journalEntryId": {"bsonType": "string"},
            "entityReference": {
                "bsonType": "object",
                "required": ["entityType", "entityId"],
                "properties": {
                    "entityType": {"bsonType": "string"},
                    "entityId": {"bsonType": "string"},
                },
            },
            "sourceReference": _SOURCE_REF_SCHEMA,
        },
    }
}

# Pacioli balance invariant: Σ DEBIT amount == Σ CREDIT amount per document.
# Enforced at the DB level so any write that bypasses the service layer is rejected.
# $map extracts the `amount` field from each filtered leg before $sum aggregates.
_JOURNAL_BALANCE_VALIDATOR = {
    "$expr": {
        "$eq": [
            {
                "$sum": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$entries",
                                "as": "e",
                                "cond": {"$eq": ["$$e.side", "DEBIT"]},
                            }
                        },
                        "as": "e",
                        "in": "$$e.amount",
                    }
                }
            },
            {
                "$sum": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$entries",
                                "as": "e",
                                "cond": {"$eq": ["$$e.side", "CREDIT"]},
                            }
                        },
                        "as": "e",
                        "in": "$$e.amount",
                    }
                }
            },
        ]
    }
}


def _ensure(connection: MongoDBConnection, db_name: str, collection: str, specs: list[dict]) -> list[str]:
    coll = connection.get_collection(db_name, collection)
    ensured = []
    for spec in specs:
        opts = {k: v for k, v in spec.items() if k not in ("name", "keys")}
        coll.create_index(spec["keys"], name=spec["name"], **opts)
        ensured.append(spec["name"])
    return ensured


def ensure_gl_accounts_indexes(connection: MongoDBConnection, db_name: str) -> list[str]:
    return _ensure(connection, db_name, "glAccounts", GL_ACCOUNTS_INDEXES)


def ensure_validators(connection: MongoDBConnection, db_name: str) -> list[str]:
    """Apply collection-level validators. Idempotent — re-running replaces the validator in place."""
    db = connection.client[db_name]
    specs = [
        ("ledgerEvents", _LEDGER_EVENTS_VALIDATOR),
        ("subLedgerEntries", _SUBLEDGER_ENTRIES_VALIDATOR),
        ("journalEntries", _JOURNAL_BALANCE_VALIDATOR),
    ]
    applied = []
    for coll_name, validator in specs:
        db.command("collMod", coll_name, validator=validator, validationAction="error")
        applied.append(coll_name)
    return applied


def ensure_ledger_indexes(connection: MongoDBConnection, db_name: str) -> dict[str, list[str]]:
    """Ensure indexes and validators for all ledger collections. Returns a collection→names map."""
    return {
        "glAccounts": _ensure(connection, db_name, "glAccounts", GL_ACCOUNTS_INDEXES),
        "ledgerEvents": _ensure(connection, db_name, "ledgerEvents", LEDGER_EVENTS_INDEXES),
        "subLedgerEntries": _ensure(connection, db_name, "subLedgerEntries", SUBLEDGER_ENTRIES_INDEXES),
        "journalEntries": _ensure(connection, db_name, "journalEntries", JOURNAL_ENTRIES_INDEXES),
        "ledgerStreamTokens": _ensure(connection, db_name, "ledgerStreamTokens", STREAM_TOKENS_INDEXES),
        "validators": ensure_validators(connection, db_name),
    }


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set. Create backend/.env (see README).")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    connection = MongoDBConnection(uri)
    results = ensure_ledger_indexes(connection, db_name)
    for coll, names in results.items():
        logger.info("%s indexes ensured on %s: %s", coll, db_name, names)


if __name__ == "__main__":
    main()