"""Write the posting fact back onto `payments` after a journal is posted (stage 6, doc 20 B1).

## Why the ledger service is allowed to write `payments`

The canonical spec assigns these fields to this service in three separate field descriptions:

  * `lifecycle.postingStatus` — *"Accounting axis. Advances independently of currentState
    because POSTED is an accounting fact, not a pipeline position. Owned by the ledger
    service via CDC, never written by the payment path."*
  * `refs.ledgerEventId` — *"Written by the ledger service, not the payment path."*
  * `refs.journalEntryId` — *"Written back after batch posting."*

So this is not a firewall breach. The rule that **does** still hold, unchanged, is that the
accounting *derivation* path — `ingest_worker` -> `projection_worker` -> `gl_batch` ->
`journal_service` — never reads `payments`. This module writes it and never reads it for any
accounting purpose: the `paymentId`s come from the `ledgerEvents` the batch already holds.
`test_the_derivation_path_never_reads_payments` guards that line.

## Why it runs OUTSIDE the journal's ACID transaction

Deliberately the opposite call from the balance snapshot (B2), which runs inside it. A
balance is an accounting fact derived only from the journal, so it belongs in the journal's
transaction. `payments` is an *operational* record owned by another service; letting a write
to it roll back a balanced, reconciled journal would couple accounting to operations, which
is exactly what the async-CDC GL design exists to prevent (decisions.md 2026-06-18).

Consequence, stated plainly: the GL is the system of record and the payment's copy is a
convenience pointer. A failure here logs at `error` and leaves a stale pointer. It never
loses or blocks a journal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

# Mirrors `payment_order_initiation.domain.lifecycle` in the transactions service. NOT
# imported: that module is in another service, and its own docstring says "nothing writes
# payments.status or payments.lifecycle except this module". Mirroring the file would create
# a fourth mirror-drift surface (defects.md `mirror-drift`); an HTTP call would create the
# first service-to-service dependency in the system (architecture.md: "no service-to-service
# HTTP calls — the UI is the integrator"). So we write the two fields directly, and
# `test_the_written_event_matches_the_state_machines_own_shape` asserts the shapes agree.
_IN_PROGRESS = "IN_PROGRESS"
_POSTED = "POSTED"
_ACTOR = "ledger-service"
_ACTOR_TYPE = "SERVICE"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def payment_ids_for_events(
    event_ids: list[str],
    connection: MongoDBConnection,
    db_name: str,
) -> dict[str, str]:
    """Map `eventId` -> `paymentId`, read from `ledgerEvents` (not from `payments`).

    `ingest_worker` sets `idempotencyKey = paymentId`, which is the only link the ledger
    holds and needs.
    """
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    out: dict[str, str] = {}
    for ev in le_coll.find(
        {"eventId": {"$in": event_ids}},
        {"_id": 0, "eventId": 1, "idempotencyKey": 1},
    ):
        payment_id = ev.get("idempotencyKey")
        if payment_id:
            out[ev["eventId"]] = payment_id
    return out


def write_back(
    journal_id: str,
    event_ids: list[str],
    connection: MongoDBConnection,
    db_name: str,
) -> int:
    """Stamp the posting fact on every payment this journal posted. Returns the count updated.

    Never raises. Idempotent: a replayed batch re-sets the same values and appends no second
    lifecycle event, because the `POSTED` transition is guarded on `currentState`.
    """
    try:
        return _write_back(journal_id, event_ids, connection, db_name)
    except Exception:  # noqa: BLE001 — see the module docstring: a stale pointer, never a lost journal
        logger.exception(
            "posting write-back failed for journal %s; the journal itself is committed and "
            "correct. payments.refs.journalEntryId will be stale until the next post.",
            journal_id,
        )
        return 0


def _write_back(
    journal_id: str,
    event_ids: list[str],
    connection: MongoDBConnection,
    db_name: str,
) -> int:
    by_event = payment_ids_for_events(event_ids, connection, db_name)
    if not by_event:
        logger.warning(
            "journal %s posted but no ledgerEvent carried a paymentId — nothing to write back",
            journal_id,
        )
        return 0

    payments = connection.get_collection(db_name, "payments")
    txn_coll = connection.get_collection(db_name, "transactions")
    now = _now_utc()
    updated = 0

    for event_id, payment_id in sorted(by_event.items()):
        # ⚠️ The field is `txnId`, NOT `transactionId`. The spec's own description of
        # `payments.refs.transactionId` says *"FK -> transactions.transactionId"*, but the
        # collection has always stored `txnId` (`payment_rail/documents.py:45`) — the
        # `transactionId` key on that line's sibling belongs to the *notifications* doc.
        # Reading the spec's name here left `refs.transactionId` permanently null while the
        # other three fields wrote correctly (found on the live cluster, 2026-09-02).
        # `transactionId` is kept as a fallback so this keeps working if the collection is
        # ever renamed to match the spec. Naming mismatch raised as Q49.
        txn = txn_coll.find_one(
            {"paymentId": payment_id}, {"_id": 0, "txnId": 1, "transactionId": 1}
        )
        fields = {
            "lifecycle.postingStatus": _POSTED,
            "refs.journalEntryId": journal_id,
            "refs.ledgerEventId": event_id,
            "updatedAt": now,
        }
        txn_ref = (txn or {}).get("txnId") or (txn or {}).get("transactionId")
        if txn_ref:
            fields["refs.transactionId"] = txn_ref
        else:
            logger.warning(
                "no transactions doc with a txnId for paymentId=%s — refs.transactionId "
                "left unset", payment_id,
            )

        # Two writes, deliberately separate. The first is unconditional: the posting axis
        # advances regardless of where the payment sits in the pipeline, which is the whole
        # point of `postingStatus` being a second axis (spec: "advances independently of
        # currentState"). The second is guarded, because `currentState` may legitimately
        # already be SETTLED — an internal transfer settles synchronously and posts ten
        # minutes later, so POSTED genuinely arrives after SETTLED (lifecycle.py:82-84).
        result = payments.update_one({"paymentId": payment_id}, {"$set": fields})
        if result.matched_count:
            updated += 1

        payments.update_one(
            {"paymentId": payment_id, "lifecycle.currentState": _IN_PROGRESS},
            {
                "$set": {
                    "lifecycle.currentState": _POSTED,
                    "lifecycle.stateEnteredAt": now,
                    "status": _POSTED,
                    "updatedAt": now,
                },
                "$push": {"lifecycle.events": {
                    "state": _POSTED,
                    "at": now,
                    "actor": _ACTOR,
                    "actorType": _ACTOR_TYPE,
                    "reason": f"Posted to the general ledger by {journal_id}",
                }},
            },
        )

    logger.info(
        "journal %s written back to %d payment(s)", journal_id, updated,
    )
    return updated
