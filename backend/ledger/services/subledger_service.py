"""SubLedger service — Stage ② write logic.

Pure builder + DB writer for the ledgerEvent → subLedgerEntries fan-out. The
projection worker calls these; keeping the logic here makes it unit-testable
without a change stream.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import Decimal128, ObjectId
from pymongo.errors import BulkWriteError, DuplicateKeyError

from database.connection import MongoDBConnection
from shared.refs import PREFIX_SUBLEDGER_ENTRY, derive_ref

logger = logging.getLogger(__name__)

# BIAN SourceSystemReference / SourceTransactionType constants for subLedgerEntries.
_SOURCE_SYSTEM = "LEDGER_PIPELINE"
_SOURCE_TYPE_LEDGER_EVENT = "LEDGER_EVENT"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_subledger_entries(event: dict) -> list[dict]:
    """Pure: fan out one ledgerEvent into two subLedgerEntry dicts (DR + CR)."""
    event_id = event["eventId"]
    sub_ledger_type = (event.get("meta") or {}).get("subLedgerType", "CUSTOMER_DEPOSITS")
    occurred_at = event.get("occurredAt") or _now_utc()
    value_date = event.get("valueDate") or occurred_at
    mapping_version = event.get("mappingVersion", "")
    period_code = value_date.strftime("%Y-%m")
    now = _now_utc()

    entries = []
    for side, leg_key in [("DEBIT", "debitLeg"), ("CREDIT", "creditLeg")]:
        leg = event[leg_key]
        sl_oid = ObjectId()
        entries.append({
            "_id": sl_oid,
            "subLedgerId": derive_ref(PREFIX_SUBLEDGER_ENTRY, sl_oid),
            "idempotencyKey": f"{event_id}-{side}",
            "controlAccountCode": leg["controlAccountCode"],
            "subLedgerType": sub_ledger_type,
            "entityReference": leg["entityReference"],
            "side": side,
            "amount": leg["amount"],
            "currency": leg["currency"],
            "functionalAmount": leg["amount"],
            "periodCode": period_code,
            "mappingVersion": mapping_version,
            "status": "POSTED",
            "sourceReference": {
                "sourceCollection": "ledgerEvents",
                "sourceId": event_id,
                "sourceSystem": _SOURCE_SYSTEM,
                "sourceType": _SOURCE_TYPE_LEDGER_EVENT,
            },
            "valueDate": value_date.isoformat(),              # string per v30
            "postingDate": now.isoformat(),                   # string per v30
            "journalEntryId": "",                             # sentinel: v30 requires string, not null
            "createdAt": now.isoformat(),                     # string per v30
            "updatedAt": now.isoformat(),
        })
    return entries


def _to_int(val) -> int:
    """Coerce a MongoDB numeric (Decimal128, Decimal, float, int) to int."""
    if isinstance(val, Decimal128):
        return int(val.to_decimal())
    return int(val)


def _validate_entity_references(
    entries: list[dict],
    connection: MongoDBConnection,
    db_name: str,
) -> None:
    """Reject postings against missing or inactive entities (design Step 4, Note 7).

    Only ACCOUNT entityType is validated against the accounts collection. Other
    entity types (loan, customer) are logged and passed through until those
    collections exist.
    """
    accounts_coll = connection.get_collection(db_name, "accounts")
    for entry in entries:
        ref = entry.get("entityReference") or {}
        entity_type = ref.get("entityType")
        entity_id = ref.get("entityId")
        if not entity_id:
            raise ValueError(
                f"missing entityReference.entityId on {entry.get('side')} leg"
            )
        if entity_type != "ACCOUNT":
            logger.warning(
                "entityType=%r not validated (only ACCOUNT supported); proceeding", entity_type
            )
            continue
        acct = accounts_coll.find_one({"accountId": entity_id}, projection={"status": 1})
        if acct is None:
            raise ValueError(f"entityId={entity_id!r} not found in accounts")
        if acct.get("status") != "ACTIVE":
            raise ValueError(
                f"entityId={entity_id!r} is not ACTIVE (status={acct.get('status')!r})"
            )


def _fetch_running_balance(sl_coll, control_account_code: str) -> int:
    """Last running balance for the account, or 0 if no prior entries exist.

    postingDate is an ISO-8601 UTC string — lexicographic sort equals chronological sort
    because all values use a consistent +00:00 offset and zero-padded components.
    The _id tiebreaker makes "last" deterministic within the same posting minute.
    """
    last = sl_coll.find_one(
        {"controlAccountCode": control_account_code},
        sort=[("postingDate", -1), ("_id", -1)],
        projection={"runningBalance": 1},
    )
    return _to_int(last["runningBalance"]) if last and "runningBalance" in last else 0


def assign_running_balances(entries: list[dict], starting_by_account: dict[str, int]) -> None:
    """Stamp ``runningBalance`` on each entry, chaining per control account.

    Pure: walks entries DEBIT-first and accumulates the signed delta per control account on
    top of that account's starting balance. When both legs hit the same control account (an
    internal deposit->deposit transfer), the second leg sees the first leg's effect, so the
    net-zero transfer leaves the account's last running balance unchanged.
    """
    running = dict(starting_by_account)
    for entry in sorted(entries, key=lambda e: 0 if e["side"] == "DEBIT" else 1):
        code = entry["controlAccountCode"]
        amount = _to_int(entry["amount"])
        delta = amount if entry["side"] == "DEBIT" else -amount
        running[code] = running.get(code, 0) + delta
        entry["runningBalance"] = running[code]


def write_subledger_entries(
    entries: list[dict],
    event_id: str,
    connection: MongoDBConnection,
    db_name: str,
) -> None:
    """Insert the two sub-ledger entries and stamp ledgerEvents.postingResult.

    The insert_many and ledgerEvents status update are committed in one ACID
    transaction so a crash between them never leaves a half-projected event.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    le_coll = connection.get_collection(db_name, "ledgerEvents")

    _validate_entity_references(entries, connection, db_name)

    debit_entry = next(e for e in entries if e["side"] == "DEBIT")
    credit_entry = next(e for e in entries if e["side"] == "CREDIT")

    # Gap 9: compute running balance before the transaction (eventually consistent by design).
    # Fetch each distinct control account's prior balance once, then chain per account so two
    # legs on the same account don't both read the same pre-insert value.
    starting_by_account = {
        code: _fetch_running_balance(sl_coll, code)
        for code in {e["controlAccountCode"] for e in entries}
    }
    assign_running_balances(entries, starting_by_account)

    try:
        with connection.client.start_session() as session:
            with session.start_transaction():
                sl_coll.insert_many(entries, ordered=True, session=session)
                # postingStatus stays PENDING here — per schema, POSTED means both the
                # subLedgerEntry AND journalEntry are written, so gl_batch flips it at Step 3.
                le_coll.update_one(
                    {"eventId": event_id},
                    {"$set": {
                        "postingResult": {
                            "subLedgerIdDebit": debit_entry["subLedgerId"],
                            "subLedgerIdCredit": credit_entry["subLedgerId"],
                            "postedAt": _now_utc(),
                        },
                    }},
                    session=session,
                )
    except DuplicateKeyError:
        logger.info("subLedgerEntries already exist for eventId=%s; skipping", event_id)
        return
    except BulkWriteError as exc:
        if all(e.get("code") == 11000 for e in exc.details.get("writeErrors", [])):
            logger.info("subLedgerEntries already exist for eventId=%s; skipping", event_id)
            return
        raise

    logger.info("projected %d subLedgerEntries for eventId=%s", len(entries), event_id)
