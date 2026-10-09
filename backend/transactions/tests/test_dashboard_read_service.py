"""Tests for the dashboard read service.

Hermetic: the fake collections return canned aggregate rows, so these tests cover the
shaping (zero-fill, stage buckets, KPI comparison), not the Mongo pipelines themselves.
"""

from datetime import datetime, timedelta, timezone

from contexts.payment_order_initiation.domain import lifecycle
from services import dashboard_read_service as svc

NOW = datetime(2026, 9, 16, 10, 24, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def test_bucket_starts_are_aligned_oldest_first():
    starts = svc.bucket_starts(NOW, timedelta(hours=24), "hour", HOUR)
    assert len(starts) == 24
    assert starts[-1] == datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    assert starts[0] == datetime(2026, 9, 15, 11, 0, tzinfo=timezone.utc)


def test_fill_series_zero_fills_and_totals():
    starts = svc.bucket_starts(NOW, timedelta(hours=3), "hour", HOUR)
    rows = [
        {"_id": {"t": starts[1], "k": "WIRE"}, "n": 4},
        {"_id": {"t": starts[1], "k": "INTERNAL"}, "n": 1},
    ]
    points = svc.fill_series(starts, rows, ["WIRE", "INTERNAL"])
    assert [p["total"] for p in points] == [0, 5, 0]
    assert points[1]["WIRE"] == 4 and points[0]["WIRE"] == 0


def test_fill_series_accepts_naive_datetimes_from_the_driver():
    starts = svc.bucket_starts(NOW, timedelta(hours=1), "hour", HOUR)
    rows = [{"_id": {"t": starts[0].replace(tzinfo=None), "k": "count"}, "n": 2}]
    assert svc.fill_series(starts, rows, ["count"])[0]["count"] == 2


def test_stage_breakdown_sums_to_the_total_including_unknown_states():
    by_status = {lifecycle.SETTLED: 5, lifecycle.REJECTED: 2, lifecycle.PENDING_FUNDS: 1, "NEW_STATE": 3}
    stages = {s["stage"]: s["count"] for s in svc.stage_breakdown(by_status)}
    assert stages["Settled"] == 5 and stages["Exception"] == 2
    assert stages["On hold"] == 1 and stages["Other"] == 3
    assert sum(stages.values()) == sum(by_status.values())


def test_every_lifecycle_state_is_in_exactly_one_stage_bucket():
    seen = [s for _, states in svc.STAGE_BUCKETS for s in states]
    assert len(seen) == len(set(seen))


class _Coll:
    def __init__(self, handler, docs=()):
        self.handler = handler
        self.docs = list(docs)

    def aggregate(self, pipeline):
        return self.handler(pipeline)

    def find(self, *a, **k):
        return _Cursor(self.docs)


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_):
        return self

    def limit(self, n):
        return iter(self.docs[:n])


class _Conn:
    def __init__(self, payments, exceptions, settlement=None):
        self.c = {"payments": payments, "exceptions": exceptions,
                  "settlementPositions": settlement or _Coll(lambda p: [])}

    def get_collection(self, _db, name):
        return self.c[name]


def test_get_dashboard_shapes_kpis_with_previous_window(monkeypatch):
    monkeypatch.setattr(svc.workflow_read_service, "list_exceptions", lambda *a, **k: {"items": []})
    monkeypatch.setattr(svc.workflow_read_service, "list_payments", lambda *a, **k: {"items": []})
    calls = {"status": 0}

    def payments(pipeline):
        group = pipeline[1].get("$group")
        if group is None:  # dwell / reconciliation pipelines are covered separately
            return []
        if group["_id"] == "$status":
            calls["status"] += 1
            if calls["status"] == 1:  # current window
                return [{"_id": lifecycle.SETTLED, "n": 8}, {"_id": lifecycle.REJECTED, "n": 1},
                        {"_id": lifecycle.IN_PROGRESS, "n": 1}]
            return [{"_id": lifecycle.SETTLED, "n": 5}]  # previous window
        if group["_id"] == "$lifecycle.reconciliationStatus":
            return []
        if group["_id"] == "$rail":
            return [{"_id": "WIRE", "n": 10}]
        return [{"_id": {"t": datetime(2026, 9, 16, 10, tzinfo=timezone.utc), "k": "WIRE"}, "n": 10}]

    def exceptions(pipeline):
        group = pipeline[1]["$group"]
        if "$ne" in str(pipeline[0]["$match"].get("agent", "")):
            return [{"_id": True, "n": 2}, {"_id": False, "n": 1}]
        if group["_id"] == "$category":
            return [{"_id": "RECONCILIATION_DISCREPANCY", "n": 3}]
        return []

    agent_doc = {"exceptionId": "EXC-1", "paymentId": "PAY-1", "category": "RECONCILIATION_DISCREPANCY",
                 "status": "RESOLVED", "agent": {"rootCause": "Fee deducted", "confidence": "HIGH",
                                                 "verification": {"result": "RESOLVED"}}}
    out = svc.get_dashboard(_Conn(_Coll(payments), _Coll(exceptions, [agent_doc])), "db", window="24h", now=NOW)
    assert out["agentImpact"]["involved"] == 3 and out["agentImpact"]["resolved"] == 2
    assert out["agentImpact"]["successRate"] == 66.7 and out["kpis"]["resolvedByAgents"] == 2
    assert out["agentImpact"]["recent"][0]["rootCause"] == "Fee deducted"

    assert out["kpis"]["total"] == 10 and out["kpis"]["completed"] == 8
    assert out["kpis"]["exceptions"] == 1 and out["kpis"]["inProgress"] == 1
    assert out["kpis"]["successRate"] == 80.0
    assert out["kpis"]["previous"]["total"] == 5
    assert out["types"] == [{"type": "WIRE", "count": 10}]
    assert len(out["volume"]["series"]) == 24 and out["volume"]["series"][-1]["WIRE"] == 10
    assert out["exceptionReasons"] == [{"reason": "RECONCILIATION_DISCREPANCY", "count": 3}]


def test_empty_window_has_no_success_rate(monkeypatch):
    monkeypatch.setattr(svc.workflow_read_service, "list_exceptions", lambda *a, **k: {"items": []})
    monkeypatch.setattr(svc.workflow_read_service, "list_payments", lambda *a, **k: {"items": []})
    empty = _Coll(lambda p: [])
    out = svc.get_dashboard(_Conn(empty, empty), "db", window="7d", now=NOW)
    assert out["kpis"]["successRate"] is None and len(out["volume"]["series"]) == 7


def test_processing_times_weight_states_within_a_stage():
    rows = [
        {"_id": lifecycle.VALIDATED, "avgMs": 2000, "n": 3},
        {"_id": lifecycle.FINAL_VALIDATED, "avgMs": 4000, "n": 1},
        {"_id": lifecycle.REJECTED, "avgMs": 9000, "n": 1},
    ]
    out = {r["stage"]: r for r in svc.processing_times(rows)}
    assert out["Validated"]["avgSeconds"] == 2.5 and out["Validated"]["samples"] == 4
    assert "Exception" not in out and "Settled" not in out


def test_reconciliation_counts_and_percentage():
    coll = _Coll(lambda p: [{"_id": "RECONCILED", "n": 9}, {"_id": "PENDING", "n": 1}, {"_id": "DISCREPANT", "n": 0}])
    out = svc._reconciliation(coll, {})
    assert out["reconciled"] == 9 and out["pending"] == 1 and out["reconciledPct"] == 90.0


def test_settlement_rows_are_shaped():
    rows = [{"_id": "Correspondent bank", "positions": 4, "settled": 3, "delayed": 1, "returned": 0}]
    conn = _Conn(_Coll(lambda p: []), _Coll(lambda p: []), _Coll(lambda p: rows))
    assert svc._settlement(conn, "db", {}) == [
        {"scheme": "Correspondent bank", "positions": 4, "settled": 3, "delayed": 1, "returned": 0}]


def test_dwell_pipeline_is_bounded_and_uses_the_newest_payments():
    seen = []
    coll = _Coll(lambda p: seen.append(p) or [])
    svc._dwell_rows(coll, {})
    assert {"$sort": {"createdAt": -1}} in seen[0]
    assert {"$limit": svc.DWELL_SAMPLE_LIMIT} in seen[0]


def test_cached_dashboard_computes_once_within_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "get_dashboard", lambda *a, **k: calls.append(1) or {"n": len(calls)})
    svc._cache.clear()
    first = svc.get_dashboard_cached(None, "db", window="24h")
    assert svc.get_dashboard_cached(None, "db", window="24h") is first
    svc.get_dashboard_cached(None, "db", window="7d")  # another window computes separately
    assert len(calls) == 2
    svc._cache.clear()
