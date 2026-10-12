"""`paymentHistory` — the lifecycle events and checks that used to live inside `payments`.

`payments.lifecycle.events[]` and `payments.checks[]` grew without bound. Both now live one
document per entry in `paymentHistory`, discriminated by `kind`:

* `EVENT` — one lifecycle transition: `state, at, actor, actorType, reason`.
* `CHECK` — one check outcome: the fields `checks.check()` builds.

`payments` keeps two counters, `lifecycle.eventCount` and `lifecycle.checkCount`. A writer
`$inc`s the counter in the same transaction that inserts the history documents and uses the
value it gets back as `seq`, so `seq` runs 1..N with no gaps and no second source of truth.
`_id` is `"{paymentId}:{kind}:{seq}"`: a retried insert collides instead of duplicating.

Insert-only. Never updated or deleted — a correction is a new entry, as before.

The ledger and payment_agent services keep their own copy of the shape (no cross-service
imports); the shape tests in each service assert the copies agree.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

HISTORY = "paymentHistory"
EVENT = "EVENT"
CHECK = "CHECK"

# Envelope fields on a stored document that are not part of the entry itself.
_ENVELOPE = ("_id", "paymentId", "rail", "kind", "seq")


def collection(payments):
    """The `paymentHistory` handle on the same database as `payments`."""
    return payments.database[HISTORY]


def history_id(payment_id: str, kind: str, seq: int) -> str:
    return f"{payment_id}:{kind}:{seq}"


def event_document(payment: dict, seq: int, event: dict) -> dict:
    return {
        "_id": history_id(payment["paymentId"], EVENT, seq),
        "paymentId": payment["paymentId"],
        "rail": payment.get("rail"),
        "kind": EVENT,
        "seq": seq,
        **event,
    }


def check_documents(payment: dict, entries: list) -> list:
    """History documents for `entries`, which the payment's `checkCount` was just bumped for.

    `payment` is the document returned AFTER the `$inc`, so the new entries occupy
    `checkCount - len(entries) + 1 .. checkCount`.
    """
    last = payment["lifecycle"]["checkCount"]
    first = last - len(entries) + 1
    return [
        {
            "_id": history_id(payment["paymentId"], CHECK, first + i),
            "paymentId": payment["paymentId"],
            "rail": payment.get("rail"),
            "kind": CHECK,
            "seq": first + i,
            **entry,
        }
        for i, entry in enumerate(entries)
    ]


def run_in_transaction(payments, session, work: Callable[[Any], Any]):
    """Run `work(session)`. Join the caller's transaction when it has one, else open one.

    `with_transaction` may re-run `work` after a transient error, so `work` must be safe to
    repeat: every write in it is rolled back with the aborted attempt.
    """
    if session is not None:
        return work(session)
    with payments.database.client.start_session() as own:
        return own.with_transaction(work)


def entries(history, payment_id: str, kind: str, *, upto: Optional[int] = None) -> list:
    """The `kind` entries of one payment in `seq` order, envelope fields stripped.

    `upto` is the payment's counter; entries past it belong to a write the payment document
    does not yet acknowledge and are left out.
    """
    flt: dict = {"paymentId": payment_id, "kind": kind}
    if upto is not None:
        flt["seq"] = {"$lte": upto}
    docs = sorted(history.find(flt), key=lambda d: d["seq"])
    return [{k: v for k, v in d.items() if k not in _ENVELOPE} for d in docs]


def previous_and_current_event(history, payment_id: str, event_count: int) -> list:
    """The last two events, oldest first. Shorter than two when the payment has fewer."""
    if event_count < 2:
        return []
    docs = history.find({
        "paymentId": payment_id, "kind": EVENT,
        "seq": {"$in": [event_count - 1, event_count]},
    })
    docs = sorted(docs, key=lambda d: d["seq"])
    return [{k: v for k, v in d.items() if k not in _ENVELOPE} for d in docs]


def attach(history, payment: Optional[dict]) -> Optional[dict]:
    """A copy of `payment` with `lifecycle.events[]` and `checks[]` joined back in.

    The API shape is unchanged from when both arrays were embedded; `checks` is always a list,
    as it was on every payment built since stage 2.
    """
    if payment is None:
        return None
    out = copy.deepcopy(payment)
    lifecycle = out.setdefault("lifecycle", {})
    lifecycle["events"] = entries(
        history, out["paymentId"], EVENT, upto=lifecycle.get("eventCount", 0))
    out["checks"] = entries(history, out["paymentId"], CHECK, upto=lifecycle.get("checkCount", 0))
    return out
