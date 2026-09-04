"""Tests for the workflow read service — the back-office UI's read namespace.

Hermetic, following the convention in test_payments_service.py: no Atlas, no pymongo. The
fake collection implements only the operations this service calls.

Why these cases: the service is read-only, so nothing here protects money. What it protects
is the UI contract — every filter doc 16 B2 promises must actually narrow the result, the
deep dive must survive a document written before stage 2 existed (no `checks[]`), and the
Operations lens must track the state machine rather than a hardcoded list of terminals.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from contexts.payment_order_initiation.domain import lifecycle
from services import workflow_read_service as svc


# --- fakes --------------------------------------------------------------------

def _matches(doc, flt):
    for k, cond in flt.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        val = doc
        for part in k.split("."):
            val = (val or {}).get(part) if isinstance(val, dict) else None
        if isinstance(cond, dict):
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$gte" in cond and (val is None or val < cond["$gte"]):
                return False
            if "$lte" in cond and (val is None or val > cond["$lte"]):
                return False
        elif val != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction):
        self.docs.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def skip(self, n):
        self.docs = self.docs[n:]
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)


class FakePayments:
    """Stands in for the `payments` collection.

    The projection argument is accepted and ignored — these tests assert on filtering,
    ordering and aggregation, and a faithful projection implementation would be more fake
    than the thing it fakes.
    """

    def __init__(self, docs):
        self.docs = [copy.deepcopy(d) for d in docs]

    def find(self, flt, projection=None):
        return FakeCursor([copy.deepcopy(d) for d in self.docs if _matches(d, flt)])

    def find_one(self, flt, projection=None, sort=None):
        matched = [d for d in self.docs if _matches(d, flt)]
        if sort:
            for key, direction in sort:
                matched.sort(key=lambda d: d.get(key), reverse=direction < 0)
        if not matched:
            return None
        out = copy.deepcopy(matched[0])
        out.pop("_id", None)
        return out

    def count_documents(self, flt):
        return sum(1 for d in self.docs if _matches(d, flt))

    def aggregate(self, pipeline):
        rows = [d for d in self.docs if _matches(d, pipeline[0]["$match"])]
        group = pipeline[1]["$group"]
        if group["_id"] is None:
            out = {"_id": None}
            for field, spec in group.items():
                if field == "_id":
                    continue
                src = spec["$sum"]
                out[field] = sum(1 for _ in rows) if src == 1 else sum(
                    d.get(src.lstrip("$"), 0) for d in rows
                )
            return [out] if rows else []
        key = group["_id"].lstrip("$")
        buckets = {}
        for d in rows:
            buckets[d.get(key)] = buckets.get(d.get(key), 0) + 1
        return [{"_id": k, "n": v} for k, v in buckets.items()]


class FakeArtifacts:
    """Stands in for `paymentExecutions` / `paymentMessages` — stage 5's two collections.

    Empty by default: every test in this module predates stage 5, and the point of the
    fixture is that `get_payment` still returns a document when a payment has no execution
    artifacts (an internal transfer never has any — doc 19 B4).
    """

    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query, projection=None):
        payment_id = query.get("paymentId")
        return FakeCursor([d for d in self.docs if d.get("paymentId") == payment_id])

    def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                out = copy.deepcopy(d)
                out.pop("_id", None)
                return out
        return None


class FakeConnection:
    def __init__(self, coll, executions=None, messages=None, notifications=None):
        self.coll = coll
        self.artifacts = {
            "paymentExecutions": FakeArtifacts(executions),
            "paymentMessages": FakeArtifacts(messages),
            "settlementPositions": FakeArtifacts(None),  # stage 7 — empty by default
            "notifications": FakeArtifacts(notifications),
        }

    def get_collection(self, db_name, name):
        if name in self.artifacts:
            return self.artifacts[name]
        assert name == "payments"
        return self.coll


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _payment(pid, *, status=lifecycle.SETTLED, rail="WIRE", customer="CUST-1",
             amount=100.0, days_ago=0, checks=None):
    doc = {
        "paymentId": pid,
        "createdAt": NOW - timedelta(days=days_ago),
        "customerId": customer,
        "type": "CREDIT_TRANSFER",
        "rail": rail,
        "status": status,
        "amount": amount,
        "currency": "USD",
        "lifecycle": {"currentState": status, "events": [{"state": status, "at": NOW}]},
    }
    if checks is not None:
        doc["checks"] = checks
    return doc


@pytest.fixture
def conn():
    return FakeConnection(FakePayments([
        _payment("PAY-1", status=lifecycle.SETTLED, rail="WIRE", amount=25000.0),
        _payment("PAY-2", status=lifecycle.REJECTED, rail="INTERNAL", customer="CUST-2",
                 amount=500.0, days_ago=1),
        _payment("PAY-3", status=lifecycle.INITIATED, rail="WIRE", amount=75.0, days_ago=10),
    ]))


# --- list ---------------------------------------------------------------------

def test_list_returns_all_newest_first(conn):
    out = svc.list_payments(conn, "db")
    assert [p["paymentId"] for p in out["items"]] == ["PAY-1", "PAY-2", "PAY-3"]
    assert out["total"] == 3


@pytest.mark.parametrize("kwargs,expected", [
    ({"status": lifecycle.REJECTED}, ["PAY-2"]),
    ({"rail": "WIRE"}, ["PAY-1", "PAY-3"]),
    ({"customer_id": "CUST-2"}, ["PAY-2"]),
    ({"date_from": NOW - timedelta(days=2)}, ["PAY-1", "PAY-2"]),
    ({"date_to": NOW - timedelta(days=5)}, ["PAY-3"]),
])
def test_every_filter_narrows(conn, kwargs, expected):
    """Doc 16 B2 promises status, from, to, customerId and rail. Each must actually work."""
    out = svc.list_payments(conn, "db", **kwargs)
    assert [p["paymentId"] for p in out["items"]] == expected
    assert out["total"] == len(expected)


def test_total_counts_the_filter_not_the_page(conn):
    """Pagination must not shrink `total`, or the UI cannot render a page count."""
    out = svc.list_payments(conn, "db", limit=1)
    assert len(out["items"]) == 1
    assert out["total"] == 3


# --- deep dive ----------------------------------------------------------------

def test_get_payment_returns_lifecycle_events(conn):
    payment = svc.get_payment(conn, "db", "PAY-1")
    assert payment["lifecycle"]["events"][0]["state"] == lifecycle.SETTLED


def test_get_payment_tolerates_a_pre_stage_2_document(conn):
    """The gate: a payment written before stage 2 has no `checks[]` and must still load."""
    payment = svc.get_payment(conn, "db", "PAY-1")
    assert "checks" not in payment


def test_get_payment_missing_returns_none(conn):
    assert svc.get_payment(conn, "db", "PAY-nope") is None


def test_get_payment_never_leaks_object_id():
    """Echoing a raw ObjectId into a response is the 2026-06-11 defect."""
    conn = FakeConnection(FakePayments([{**_payment("PAY-9"), "_id": object()}]))
    assert "_id" not in svc.get_payment(conn, "db", "PAY-9")


# --- exceptions ---------------------------------------------------------------

def test_exceptions_lists_only_terminal_states(conn):
    out = svc.list_exceptions(conn, "db")
    assert [p["paymentId"] for p in out["items"]] == ["PAY-2"]


def test_intervention_states_track_the_state_machine():
    """Derived from lifecycle.TERMINALS, so a new terminal cannot fall out of the lens."""
    assert set(svc.INTERVENTION_STATES) == set(lifecycle.TERMINALS)


# --- stats --------------------------------------------------------------------

def test_stats_buckets_are_disjoint_and_sum_to_total(conn):
    out = svc.get_stats(conn, "db", date_from=NOW - timedelta(days=30))
    assert out["total"] == 3
    assert out["settled"] + out["inFlight"] + out["exceptions"] == out["total"]
    assert out["settled"] == 1 and out["exceptions"] == 1 and out["inFlight"] == 1


def test_stats_sums_value(conn):
    out = svc.get_stats(conn, "db", date_from=NOW - timedelta(days=30))
    assert out["totalValue"] == pytest.approx(25575.0)


def test_stats_defaults_to_a_trailing_window(conn):
    """No dates given must not mean 'every payment ever' — the strip would be meaningless."""
    out = svc.get_stats(conn, "db")
    assert out["window"]["from"] is not None


# --- resolve_ref (command search) --------------------------------------------

def _payment_with_refs(pid, *, txnId=None, debtor_acc=None, creditor_acc=None, days_ago=0):
    doc = _payment(pid, days_ago=days_ago)
    if txnId:
        doc["txnId"] = txnId
    if debtor_acc:
        doc["debtor"] = {"accountId": debtor_acc}
    if creditor_acc:
        doc["creditor"] = {"accountId": creditor_acc}
    return doc


@pytest.fixture
def rconn():
    """Payments carrying the secondary refs the command search resolves, plus a notification."""
    return FakeConnection(
        FakePayments([
            _payment_with_refs("PAY-old", debtor_acc="ACC-X", txnId="TXN-old", days_ago=5),
            _payment_with_refs("PAY-new", debtor_acc="ACC-X", txnId="TXN-new", days_ago=0),
            _payment_with_refs("PAY-3", creditor_acc="ACC-Y", txnId="TXN-3", days_ago=2),
        ]),
        notifications=[{"notificationId": "NOTIF-1", "paymentId": "PAY-new"}],
    )


def test_resolve_pay_returns_the_payment_id(rconn):
    out = svc.resolve_ref(rconn, "db", "PAY-new")
    assert out == {"paymentId": "PAY-new", "matchedBy": "paymentId", "ref": "PAY-new"}


def test_resolve_txn_via_txnId(rconn):
    out = svc.resolve_ref(rconn, "db", "TXN-3")
    assert out == {"paymentId": "PAY-3", "matchedBy": "txnId", "ref": "TXN-3"}


def test_resolve_acc_picks_the_most_recent_payment(rconn):
    """ACC-X is on PAY-old and PAY-new — the most recent wins (the analyst wants a way in)."""
    out = svc.resolve_ref(rconn, "db", "ACC-X")
    assert out["paymentId"] == "PAY-new"
    assert out["matchedBy"] == "accountId"


def test_resolve_acc_matches_creditor_side(rconn):
    out = svc.resolve_ref(rconn, "db", "ACC-Y")
    assert out["paymentId"] == "PAY-3"


def test_resolve_notif_via_notifications(rconn):
    out = svc.resolve_ref(rconn, "db", "NOTIF-1")
    assert out == {"paymentId": "PAY-new", "matchedBy": "notificationId", "ref": "NOTIF-1"}


def test_resolve_miss_returns_none(rconn):
    assert svc.resolve_ref(rconn, "db", "PAY-nope") is None
    assert svc.resolve_ref(rconn, "db", "TXN-nope") is None


def test_resolve_unknown_prefix_returns_none(rconn):
    assert svc.resolve_ref(rconn, "db", "FOO-1") is None
