"""The migration and parity scripts (`backend/data/`) agree with the writers' shapes.

The scripts need a live cluster to run, so only their pure functions are tested: the document
they synthesise from an embedded array must be what `lifecycle_history` would have written, the
reverse must restore the array exactly, and the parity checker must catch each way a migration can
go wrong.
"""

import importlib.util
import pathlib
from datetime import datetime, timezone

from contexts.payment_order_initiation.domain import checks, lifecycle_history as lh

_DATA = pathlib.Path(__file__).resolve().parents[2] / "data"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DATA / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate = _load("migrate_history_out")
parity = _load("verify_history_parity")

AT = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
EVENTS = [
    {"state": "DRAFT", "at": AT, "actor": "a", "actorType": "SERVICE", "reason": "r1"},
    {"state": "INITIATED", "at": AT, "actor": "a", "actorType": "SERVICE", "reason": "r2"},
]
CHECKS = [checks.check("2 authenticate", "mfa", checks.PASS, at=AT)]


def _embedded():
    return {"paymentId": "PAY-1", "rail": "WIRE", "status": "INITIATED",
            "lifecycle": {"currentState": "INITIATED", "events": list(EVENTS)},
            "checks": list(CHECKS)}


def test_migrated_documents_match_what_the_writers_produce():
    docs = migrate.history_documents(_embedded())

    events = [d for d in docs if d["kind"] == "EVENT"]
    assert events == [lh.event_document({"paymentId": "PAY-1", "rail": "WIRE"}, i, e)
                      for i, e in enumerate(EVENTS, start=1)]
    [check_doc] = [d for d in docs if d["kind"] == "CHECK"]
    assert check_doc == lh.check_documents(
        {"paymentId": "PAY-1", "rail": "WIRE", "lifecycle": {"checkCount": 1}}, CHECKS)[0]


def test_reverse_restores_the_embedded_arrays_exactly():
    docs = migrate.history_documents(_embedded())

    events, found = migrate.embedded_arrays(list(reversed(docs)))

    assert events == EVENTS
    assert found == CHECKS


def test_a_payment_with_no_arrays_migrates_to_nothing():
    assert migrate.history_documents({"paymentId": "P", "lifecycle": {}}) == []


def _clean(**over):
    payment = {"paymentId": "PAY-1", "status": "INITIATED",
               "lifecycle": {"currentState": "INITIATED", "eventCount": 2, "checkCount": 1}}
    payment.update(over)
    return payment


def test_parity_passes_a_consistent_payment():
    assert parity.check_payment(_clean(), {"EVENT": [1, 2], "CHECK": [1]}) == []


def test_parity_catches_a_counter_that_drifted():
    problems = parity.check_payment(_clean(), {"EVENT": [1], "CHECK": [1]})
    assert any("eventCount" in p for p in problems)


def test_parity_catches_a_gap_in_seq():
    problems = parity.check_payment(_clean(), {"EVENT": [1, 3], "CHECK": [1]})
    assert any("seq" in p for p in problems)


def test_parity_catches_an_array_that_was_not_unset():
    payment = _clean(checks=[{}])
    payment["lifecycle"]["events"] = []

    problems = parity.check_payment(payment, {"EVENT": [1, 2], "CHECK": [1]})

    assert "embedded lifecycle.events still present" in problems
    assert "embedded checks still present" in problems


def test_parity_catches_status_drifting_from_current_state():
    problems = parity.check_payment(_clean(status="SETTLED"), {"EVENT": [1, 2], "CHECK": [1]})
    assert any("status" in p for p in problems)
