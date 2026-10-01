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
    """Stands in for `paymentExecutions` / `paymentMessages` / `exceptions` — the
    collections `get_payment` / `list_exceptions` join onto a payment.

    Empty by default: every test in this module predates stage 5/9, and the point of the
    fixture is that `get_payment` still returns a document when a payment has no
    artifacts (an internal transfer never has any — doc 19 B4) and no exception (the
    common case — doc 24 §3 step 7).
    """

    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query, projection=None):
        query = query or {}
        return FakeCursor([d for d in self.docs if self._matches(d, query)])

    def find_one(self, query, projection=None):
        for d in self.docs:
            if self._matches(d, query):
                out = copy.deepcopy(d)
                out.pop("_id", None)
                return out
        return None

    def _matches(self, doc, flt):
        for k, v in flt.items():
            actual = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and actual not in v["$in"]:
                    return False
                if "$ne" in v and actual == v["$ne"]:
                    return False
                if "$nin" in v and actual in v["$nin"]:
                    return False
            elif actual != v:
                return False
        return True


class FakeConnection:
    def __init__(self, coll, executions=None, messages=None, notifications=None,
                 routing_snapshot=None, exceptions=None):
        self.coll = coll
        self.artifacts = {
            "paymentExecutions": FakeArtifacts(executions),
            "paymentMessages": FakeArtifacts(messages),
            "settlementPositions": FakeArtifacts(None),  # stage 7 — empty by default
            "notifications": FakeArtifacts(notifications),
            "routingSnapshots": FakeArtifacts(  # stage 4 — one doc, or none pre-stage-4
                [routing_snapshot] if routing_snapshot else None),
            "exceptions": FakeArtifacts(exceptions),  # stage 9 — empty by default
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


def test_list_payments_joins_the_exception_so_activity_surfaces_it():
    """The Activity list shows the exception reason + discrepancy subline for failed/returned
    payments (the former Operations queue folded in), so list_payments joins each row with its
    open (or latest resolved) exception — null for a payment with none."""
    conn = FakeConnection(
        FakePayments([
            _payment("PAY-1", status=lifecycle.SETTLED, amount=25000.0),
            _payment("PAY-2", status=lifecycle.FAILED, amount=25000.0),
        ]),
        exceptions=[_exc_doc("PAY-2")],
    )
    out = svc.list_payments(conn, "db")
    by_id = {p["paymentId"]: p for p in out["items"]}
    # The failed payment carries its OPEN exception; the settled one has none.
    assert by_id["PAY-2"]["exception"]["exceptionId"] == "EXC-aaaa0001"
    assert by_id["PAY-2"]["exception"]["status"] == "OPEN"
    assert by_id["PAY-1"]["exception"] is None


def test_list_projection_carries_all_three_status_axes():
    """Doina (Sep 17): a single `status=SETTLED` pill reads as 'everything done' when the
    posting/settlement axes lag. The list must carry all three so the UI can show them.
    The fake collection ignores projection, so this pins the allowlist directly — a later
    stage's axis is invisible here until it is named (the inclusion-allowlist rule)."""
    for key in (
        "status",
        "lifecycle.currentState",
        "lifecycle.postingStatus",
        "lifecycle.settlementStatus",
        "lifecycle.reconciliationStatus",
    ):
        assert key in svc._LIST_PROJECTION, f"{key} dropped from the list projection"


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


def test_get_payment_attaches_the_routing_snapshot_when_present():
    """FR-4.1: the stage-4 execution-strategy decision must reach the UI. The payment doc
    carries only `wireDetails.network` + `refs.routingSnapshotId`; the full decision
    (strategy, cost, correspondent, cut-off, value date, rationale) lives on the snapshot,
    so `get_payment` joins it so the deep-dive can render it."""
    snap = {"routingSnapshotId": "RS-abc", "paymentId": "PAY-1",
            "executionStrategy": "FEDWIRE_RTGS", "clearingNetwork": "FEDWIRE",
            "rationale": "domestic urgent"}
    conn = FakeConnection(FakePayments([_payment("PAY-1")]), routing_snapshot=snap)
    payment = svc.get_payment(conn, "db", "PAY-1")
    assert payment["routingSnapshot"]["executionStrategy"] == "FEDWIRE_RTGS"
    assert payment["routingSnapshot"]["routingSnapshotId"] == "RS-abc"


def test_get_payment_attaches_none_when_no_routing_snapshot(conn):
    """A pre-stage-4 payment (INITIATED, never routed) has no snapshot — `None`, not absent,
    so the UI reads 'no routing decision yet' rather than branching on undefined."""
    payment = svc.get_payment(conn, "db", "PAY-3")
    assert payment["routingSnapshot"] is None


# --- exceptions ---------------------------------------------------------------

def test_exceptions_queue_is_driven_by_exceptions_not_terminal_state(conn):
    """2026-09-30: a terminal payment with no exception is not queue work. The old
    terminal-OR-open query never drained (every FAILED/RETURNED payment stayed listed)."""
    out = svc.list_exceptions(conn, "db")
    assert out["items"] == []
    assert out["total"] == 0


def test_intervention_states_track_the_state_machine():
    """Derived from lifecycle.TERMINALS, so a new terminal cannot fall out of the lens."""
    assert set(svc.INTERVENTION_STATES) == set(lifecycle.TERMINALS)


# --- Stage 9: the exceptions join (doc 24 §3 step 7) --------------------------

from bson import ObjectId  # noqa: E402 - local import keeps the fixture block above clean

_NOW = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


def _exc_doc(pid, *, category="SETTLEMENT_UNMATCHED", status="OPEN", exc_id="EXC-aaaa0001"):
    return {
        "_id": ObjectId(),
        "exceptionId": exc_id,
        "paymentId": pid,
        "category": category,
        "status": status,
        "severity": "ACTION_REQUIRED",
        "source": {"stage": "7 settle", "service": "transactions-service"},
        "detail": {"discrepancyAmount": 25.0, "expectedAmount": 25000.0, "actualAmount": 24975.0},
        "resolution": None,
        "agent": None,
        "createdAt": _NOW,
        "updatedAt": _NOW,
        "sourceSystem": "transactions-service",
    }


def test_list_exceptions_joins_the_open_exception_per_payment():
    conn = FakeConnection(
        FakePayments([_payment("PAY-9", status=lifecycle.FAILED, amount=25000.0)]),
        exceptions=[_exc_doc("PAY-9")],
    )
    out = svc.list_exceptions(conn, "db")
    assert len(out["items"]) == 1
    row = out["items"][0]
    assert row["paymentId"] == "PAY-9"
    assert row["exception"]["exceptionId"] == "EXC-aaaa0001"
    assert row["exception"]["category"] == "SETTLEMENT_UNMATCHED"
    assert row["exception"]["status"] == "OPEN"


def test_list_exceptions_drops_a_payment_once_its_exception_is_resolved():
    """The default (OPEN) queue drains: a resolved exception's payment leaves the list."""
    conn = FakeConnection(
        FakePayments([_payment("PAY-7", status=lifecycle.FAILED)]),
        exceptions=[_exc_doc("PAY-7", status="RESOLVED", exc_id="EXC-old00007")],
    )
    out = svc.list_exceptions(conn, "db")
    assert out["items"] == [] and out["total"] == 0


def test_list_exceptions_falls_back_to_the_latest_resolved_when_no_open():
    """A closed exception still carries its history — the latest resolved/dismissed doc is
    attached when no OPEN one exists."""
    conn = FakeConnection(
        FakePayments([_payment("PAY-7", status=lifecycle.FAILED)]),
        exceptions=[_exc_doc("PAY-7", status="RESOLVED", exc_id="EXC-old00007",
                             category="RECONCILIATION_DISCREPANCY")],
    )
    out = svc.list_exceptions(conn, "db", status=None)  # history view
    row = out["items"][0]
    assert row["exception"]["exceptionId"] == "EXC-old00007"
    assert row["exception"]["status"] == "RESOLVED"


def test_list_exceptions_empty_queue_renders_without_error():
    conn = FakeConnection(FakePayments([
        _payment("PAY-1", status=lifecycle.SETTLED),  # not terminal
    ]))
    out = svc.list_exceptions(conn, "db")
    assert out["items"] == []
    assert out["total"] == 0


def test_the_joined_exception_serializes_objectid_and_datetime():
    """defect 2026-06-11 — a raw ObjectId/datetime on the joined exception must encode,
    not 500. The queue response runs through `to_json_response` (MyJSONEncoder)."""
    from routers._util import to_json_response
    conn = FakeConnection(
        FakePayments([_payment("PAY-9", status=lifecycle.FAILED)]),
        exceptions=[_exc_doc("PAY-9")],
    )
    out = svc.list_exceptions(conn, "db")
    # encodes without raising — the ObjectId _id and the datetime fields both pass through
    resp = to_json_response(out)
    assert resp.status_code == 200


def test_list_exceptions_surfaces_a_settled_payment_with_an_open_discrepancy():
    """A RECONCILIATION_DISCREPANCY exception (site 4) is stamped on a SETTLED payment —
    which is NOT a terminal state. A terminal-only queue would hide it; the exception is
    the intervention signal, so the queue surfaces the payment via its OPEN exception
    (doc 24 §3 step 7)."""
    conn = FakeConnection(
        FakePayments([
            _payment("PAY-7", status=lifecycle.SETTLED, amount=25000.0),
            _payment("PAY-2", status=lifecycle.REJECTED, amount=500.0, days_ago=1),
        ]),
        exceptions=[_exc_doc("PAY-7", category="RECONCILIATION_DISCREPANCY",
                             status="OPEN", exc_id="EXC-7")],
    )
    out = svc.list_exceptions(conn, "db")
    ids = [p["paymentId"] for p in out["items"]]
    # PAY-7 surfaces via its OPEN discrepancy (SETTLED isn't terminal); PAY-2 is REJECTED
    # with no exception — not queue work.
    assert ids == ["PAY-7"]
    seven = next(p for p in out["items"] if p["paymentId"] == "PAY-7")
    assert seven["exception"]["category"] == "RECONCILIATION_DISCREPANCY"
    assert seven["exception"]["status"] == "OPEN"



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


def test_list_exceptions_filters_by_category():
    """FR-9.IN4 — the queue is filterable by category."""
    conn = FakeConnection(
        FakePayments([_payment("PAY-7", status=lifecycle.FAILED),
                      _payment("PAY-8", status=lifecycle.SETTLED, days_ago=1)]),
        exceptions=[_exc_doc("PAY-7", category="SETTLEMENT_UNMATCHED", exc_id="EXC-7"),
                    _exc_doc("PAY-8", category="RECONCILIATION_DISCREPANCY", exc_id="EXC-8")],
    )
    out = svc.list_exceptions(conn, "db", category="RECONCILIATION_DISCREPANCY")
    assert [p["paymentId"] for p in out["items"]] == ["PAY-8"]
    assert out["total"] == 1


def test_list_exceptions_shows_an_orphan_statement_line_as_its_own_row():
    """Reconciliation plan A3 D1a — an ORPHANED_SETTLEMENT is keyed `<msgId>#<lineNo>` and
    has no payment, so the queue builds its row from the exception, not the payments join."""
    orphan = {**_exc_doc("PM-STMT0001#3", category="ORPHANED_SETTLEMENT", exc_id="EXC-orph0001"),
              "subjectRef": {"kind": "STATEMENT_LINE", "paymentMessageId": "PM-STMT0001", "lineNo": 3},
              "detail": {"actualAmount": 3400.0, "currency": "USD", "reference": "ORPH-1A2B3C4D"},
              "sourceSystem": "ledger-service"}
    conn = FakeConnection(FakePayments([]), exceptions=[orphan])

    out = svc.list_exceptions(conn, "db")

    (row,) = out["items"]
    assert out["total"] == 1
    assert row["paymentId"] == "PM-STMT0001#3"
    assert row["subjectRef"]["kind"] == "STATEMENT_LINE"
    assert row["amount"] == 3400.0 and row["status"] is None
    assert row["exception"]["exceptionId"] == "EXC-orph0001"
