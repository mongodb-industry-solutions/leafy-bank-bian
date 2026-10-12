"""`paymentHistory` — lifecycle events and checks live outside `payments`.

Pins the plan's success criteria (`.claude/outputs/plan-lifecycle-history-out.md`):
contiguous `seq` matching the counters, no embedded arrays, atomic payment-update + event
insert, no event on a lost race, and a payment document whose size does not grow with its
history. Hermetic — the FakeDb session rolls back on an exception, which is what lets the
atomicity cases be asserted.
"""

import copy
from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from contexts.payment_order_initiation.domain import checks, lifecycle, lifecycle_history as lh
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    FakeCollection,
    FakeDb,
    _initiate,
    db,       # noqa: F401 - pytest fixture
    service,  # noqa: F401 - pytest fixture
)


def _history(db, payment_id, kind):
    return sorted((d for d in db["paymentHistory"].docs
                   if d["paymentId"] == payment_id and d["kind"] == kind),
                  key=lambda d: d["seq"])


def _stored(db):
    [payment] = db["payments"].docs
    return payment


# --- a payment that went through the saga ------------------------------------

def test_the_payments_document_carries_no_arrays(service, db):  # noqa: F811
    _initiate(service)

    stored = _stored(db)
    assert "events" not in stored["lifecycle"]
    assert "checks" not in stored


def test_event_seq_is_contiguous_and_matches_the_counter(service, db):  # noqa: F811
    _initiate(service)

    stored = _stored(db)
    events = _history(db, stored["paymentId"], lh.EVENT)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert stored["lifecycle"]["eventCount"] == len(events)
    assert events[0]["state"] == "DRAFT", "the DRAFT event is inserted with the payment"
    assert events[-1]["state"] == stored["lifecycle"]["currentState"]


def test_check_seq_is_contiguous_and_matches_the_counter(service, db):  # noqa: F811
    _initiate(service)

    stored = _stored(db)
    found = _history(db, stored["paymentId"], lh.CHECK)
    assert found, "the saga records checks"
    assert [c["seq"] for c in found] == list(range(1, len(found) + 1))
    assert stored["lifecycle"]["checkCount"] == len(found)


def test_history_ids_are_deterministic(service, db):  # noqa: F811
    _initiate(service)

    for doc in db["paymentHistory"].docs:
        assert doc["_id"] == f"{doc['paymentId']}:{doc['kind']}:{doc['seq']}"


def test_attach_rebuilds_the_embedded_shape(service, db):  # noqa: F811
    _initiate(service)

    joined = lh.attach(db["paymentHistory"], _stored(db))
    assert [e["state"] for e in joined["lifecycle"]["events"]][0] == "DRAFT"
    assert set(joined["lifecycle"]["events"][0]) == {"state", "at", "actor", "actorType", "reason"}
    assert set(joined["checks"][0]) == {
        "stage", "name", "result", "mode", "detail", "field", "code", "at", "actor"}


def test_attach_ignores_history_the_payment_does_not_yet_acknowledge(service, db):  # noqa: F811
    _initiate(service)
    stored = _stored(db)
    stored["lifecycle"]["eventCount"] -= 1

    joined = lh.attach(db["paymentHistory"], stored)

    assert len(joined["lifecycle"]["events"]) == stored["lifecycle"]["eventCount"]


def test_a_long_history_does_not_grow_the_payment_document(service, db):  # noqa: F811
    _initiate(service)
    stored = _stored(db)
    # `updatedAt` moves on every write and its repr length varies, so leave it out of the size.
    sized = lambda doc: len(repr({k: v for k, v in doc.items() if k not in ("lifecycle", "updatedAt")}))
    before = sized(stored)

    for i in range(200):
        checks.append_checks(
            db["payments"], stored["_id"],
            [checks.check("9 test", f"noise_{i}", checks.PASS)])

    after = _stored(db)
    assert sized(after) == before
    assert after["lifecycle"]["checkCount"] > 200
    assert set(after["lifecycle"]) == set(stored["lifecycle"])


# --- the transaction ----------------------------------------------------------

def _drafted(db, state=lifecycle.DRAFT):
    """One bare payment at `state`, inserted the way capture does."""
    now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    doc = {"_id": "oid-1", "paymentId": "PAY-1", "rail": "WIRE", "status": state,
           "lifecycle": {"currentState": state, "stateEnteredAt": now,
                         "eventCount": 1, "checkCount": 0}}
    lifecycle.insert_payment(db["payments"], doc)
    return doc


def test_insert_payment_writes_the_payment_and_its_draft_event(db):  # noqa: F811
    _drafted(db)

    assert [e["seq"] for e in _history(db, "PAY-1", lh.EVENT)] == [1]


def test_a_failed_event_insert_rolls_the_payment_update_back(db):  # noqa: F811
    _drafted(db)
    # Pre-seed seq 2 so the event insert collides with the `_id` the advance will allocate.
    db["paymentHistory"].docs.append({"_id": "PAY-1:EVENT:2", "paymentId": "PAY-1",
                                      "kind": lh.EVENT, "seq": 2})
    before = copy.deepcopy(db["payments"].docs)

    with pytest.raises(DuplicateKeyError):
        lifecycle.advance(db["payments"], "oid-1", lifecycle.INITIATED,
                          actor="t", reason="r", from_state=lifecycle.DRAFT)

    assert db["payments"].docs == before, "state, status and counter must not move"


def test_a_lost_race_inserts_no_event_and_allocates_no_seq(db):  # noqa: F811
    _drafted(db)
    lifecycle.advance(db["payments"], "oid-1", lifecycle.INITIATED,
                      actor="a", reason="first", from_state=lifecycle.DRAFT)

    with pytest.raises(lifecycle.IllegalTransition):
        lifecycle.advance(db["payments"], "oid-1", lifecycle.INITIATED,
                          actor="b", reason="second", from_state=lifecycle.DRAFT)

    events = _history(db, "PAY-1", lh.EVENT)
    assert [e["state"] for e in events] == ["DRAFT", "INITIATED"]
    assert db["payments"].docs[0]["lifecycle"]["eventCount"] == 2


def test_a_failed_capture_leaves_no_orphan_draft_event(db):  # noqa: F811
    db["payments"].unique_on = "idempotency.idempotencyKey"
    first = {"_id": "oid-1", "paymentId": "PAY-1", "rail": "WIRE",
             "idempotency": {"idempotencyKey": "K"},
             "lifecycle": {"stateEnteredAt": datetime.now(timezone.utc), "eventCount": 1}}
    lifecycle.insert_payment(db["payments"], first)
    dup = {**first, "_id": "oid-2", "paymentId": "PAY-2"}

    with pytest.raises(DuplicateKeyError):
        lifecycle.insert_payment(db["payments"], dup)

    assert {d["paymentId"] for d in db["paymentHistory"].docs} == {"PAY-1"}


def test_appended_checks_take_the_next_seq_range(db):  # noqa: F811
    _drafted(db)

    checks.append_checks(db["payments"], "oid-1", [checks.check("s", "a", checks.PASS),
                                                    checks.check("s", "b", checks.PASS)])
    checks.append_checks(db["payments"], "oid-1", [checks.check("s", "c", checks.PASS)])

    found = _history(db, "PAY-1", lh.CHECK)
    assert [(c["seq"], c["name"]) for c in found] == [(1, "a"), (2, "b"), (3, "c")]
    assert db["payments"].docs[0]["lifecycle"]["checkCount"] == 3


def test_an_empty_check_list_writes_nothing(db):  # noqa: F811
    _drafted(db)
    before = copy.deepcopy(db["payments"].docs)

    checks.append_checks(db["payments"], "oid-1", [])

    assert db["payments"].docs == before


def test_a_caller_session_is_joined_not_replaced(db):  # noqa: F811
    _drafted(db)
    seen = []

    class Spy:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def with_transaction(self, callback):  # would be used if advance opened its own
            seen.append("own")
            return callback(self)

    session = Spy()
    lifecycle.advance(db["payments"], "oid-1", lifecycle.INITIATED, actor="t", reason="r",
                      from_state=lifecycle.DRAFT, session=session)

    assert seen == []
    assert [e["seq"] for e in _history(db, "PAY-1", lh.EVENT)] == [1, 2]


# --- the collection's own definition ------------------------------------------

def test_history_index_orders_a_payments_entries_by_seq():
    from data import ensure_indexes
    [spec] = ensure_indexes.PAYMENT_HISTORY_INDEXES
    assert spec["keys"] == [("paymentId", 1), ("kind", 1), ("seq", 1)]
    assert spec["unique"] is True


def test_history_validator_requires_what_the_writers_always_set():
    from data import ensure_indexes
    schema = ensure_indexes.PAYMENT_HISTORY_VALIDATOR["$jsonSchema"]
    event_branch, check_branch = schema["oneOf"]
    assert set(event_branch["required"]) == {"state", "actor", "actorType", "reason"}
    assert set(check_branch["required"]) == {"stage", "name", "result", "mode", "actor"}
    # Every field a writer produces for each kind is covered by the branch for that kind.
    written_event = {"state", "at", "actor", "actorType", "reason"}
    assert set(event_branch["required"]) <= written_event
    written_check = set(checks.check("s", "n", checks.PASS))
    assert set(check_branch["required"]) <= written_check
    assert set(check_branch["properties"]["result"]["enum"]) == set(checks.RESULTS)
    assert set(check_branch["properties"]["mode"]["enum"]) == set(checks.MODES)
