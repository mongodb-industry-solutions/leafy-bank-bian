"""The transactions client the Cutoff Agent's gated actions call, and the cutoffCases indexes."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from types import SimpleNamespace

import pytest

import clients
import cutoff_rules as rules
import transactions_client as tc
from tests import _transactions


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    log = []

    def urlopen(req, timeout=None):
        log.append((req.full_url, json.loads(req.data)))
        return _Resp(b'{"status": "ROUTED"}')
    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("TRANSACTIONS_BASE_URL", "http://txn/")
    return log


def test_hold_next_value_date_route_and_body(sent):
    assert tc.hold_next_value_date("PAY-1", decided_by="kiran", reason="CUT-1: short") == \
        {"status": "ROUTED"}
    assert sent == [("http://txn/PaymentOrderProcedure/HoldNextValueDate",
                     {"paymentId": "PAY-1", "decidedBy": "kiran", "reason": "CUT-1: short"})]


@pytest.mark.parametrize("decision", [tc.EXPEDITE, tc.NEXT_VALUE_DATE])
def test_cutoff_decision_route_and_body(sent, decision):
    tc.cutoff_decision("PAY-1", decision=decision, decided_by="kiran")
    assert sent == [("http://txn/PaymentOrderProcedure/CutoffDecision",
                     {"paymentId": "PAY-1", "decision": decision, "decidedBy": "kiran"})]


def test_routes_match_the_rules_table(sent):
    tc.hold_next_value_date("P", decided_by="x", reason="r")
    tc.cutoff_decision("P", decision=rules.ROUTE_FOR[rules.EXPEDITE]["body"]["decision"],
                       decided_by="x")
    tc.cutoff_decision("P", decision=rules.ROUTE_FOR[rules.DEFER_NEXT_BUSINESS_DAY]["body"]["decision"],
                       decided_by="x")
    paths = [u.removeprefix("http://txn") for u, _ in sent]
    assert paths == [rules.ROUTE_FOR[a]["path"] for a in
                     (rules.HOLD_NEXT_VALUE_DATE, rules.EXPEDITE, rules.DEFER_NEXT_BUSINESS_DAY)]
    assert [b["decision"] for _, b in sent[1:]] == ["EXPEDITE", "NEXT_VALUE_DATE"]


def test_a_route_guard_is_a_refusal(monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "x", {},
                                     io.BytesIO(b'{"detail": "Past the external cut-off"}'))
    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    with pytest.raises(clients.ServiceRefused) as e:
        tc.cutoff_decision("PAY-1", decision=tc.EXPEDITE, decided_by="kiran")
    assert e.value.status == 400 and "external" in e.value.detail


# --- cutoffCases indexes (transactions ensure_indexes, loaded by path) ---------------------

@pytest.fixture
def ensure_indexes(monkeypatch):
    # The transactions `database` package would collide with payment_agent's own.
    monkeypatch.setitem(sys.modules, "database.connection",
                        SimpleNamespace(MongoDBConnection=object))
    return _transactions.load("data/ensure_indexes.py", "ensure_indexes")


def test_cutoff_case_indexes_are_declared(ensure_indexes):
    specs = {s["name"]: s for s in ensure_indexes.CUTOFF_CASES_INDEXES}
    assert specs["idx_cutoff_case_id_unique"]["unique"] is True
    active = specs["idx_cutoff_case_active_unique"]
    assert active["keys"] == [("paymentId", 1), ("valueDate", 1)] and active["unique"] is True
    assert active["partialFilterExpression"] == {"active": {"$eq": True}}
    assert specs["idx_cutoff_case_run_status"]["keys"] == \
        [("clockRunId", 1), ("status", 1), ("updatedAt", -1)]
    assert specs["idx_cutoff_case_ttl"]["expireAfterSeconds"] == 7 * 86400


def test_registry_ensures_cutoff_cases(ensure_indexes, monkeypatch):
    seen = []
    monkeypatch.setattr(ensure_indexes, "_ensure",
                        lambda conn, db_name, coll, specs: seen.append(coll) or [])
    monkeypatch.setattr(ensure_indexes, "_ensure_stage_events", lambda *a: [])
    ensure_indexes.ensure_transactions_indexes(None, "db")
    assert "cutoffCases" in seen
