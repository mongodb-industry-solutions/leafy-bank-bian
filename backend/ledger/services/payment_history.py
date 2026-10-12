"""Write a lifecycle `EVENT` into `paymentHistory` alongside the `payments` update it belongs to.

The ledger advances `payments.lifecycle.currentState` to POSTED and RECONCILED (spec-mandated,
see `posting_writeback_service`). `lifecycle.events[]` no longer lives on `payments`: each event
is a `paymentHistory` document (`kind: "EVENT"`, `seq` from `lifecycle.eventCount`). The
transactions service owns that design (`lifecycle_history.py`); this module mirrors only the
document shape, because services never import each other. `test_stage_six` and
`test_stage_eight` assert the two shapes agree.
"""

from __future__ import annotations

from typing import Optional

from pymongo import ReturnDocument

HISTORY = "paymentHistory"
EVENT = "EVENT"


def advance_with_event(payments, query: dict, set_fields: dict, event: dict) -> Optional[dict]:
    """Guarded `payments` update plus its `EVENT` insert, in one transaction.

    Returns the updated payment, or None when `query` matched nothing (a lost race or an
    ineligible state): nothing is written and no `seq` is allocated.
    """
    database = payments.database

    def work(session):
        updated = payments.find_one_and_update(
            query,
            {"$set": set_fields, "$inc": {"lifecycle.eventCount": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if updated is None:
            return None
        seq = updated["lifecycle"]["eventCount"]
        database[HISTORY].insert_one(
            {
                "_id": f"{updated['paymentId']}:{EVENT}:{seq}",
                "paymentId": updated["paymentId"],
                "rail": updated.get("rail"),
                "kind": EVENT,
                "seq": seq,
                **event,
            },
            session=session,
        )
        return updated

    # `with_transaction` retries on TransientTransactionError (a write conflict with another
    # writer of the same payment). A bare `start_transaction` would surface that as an
    # exception and lose the transition. `work` only touches the DB, so a retry is safe.
    with database.client.start_session() as session:
        return session.with_transaction(work)
