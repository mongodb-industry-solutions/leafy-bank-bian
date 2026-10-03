"""Reconciliation Agent (recon plan Part C): policy, evidence scoring, graph, worker.

The LLM is a scripted fake that emits the tool calls a real run would; the service routes are
patched at `clients`. What is pinned: the permitted-action table and its enforcement at both
boundaries, the deterministic candidate scoring, the approve → execute → verify path, the
autonomous recheck path, and the worker's filters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import clients
import policy
import reconciliation_agent as ra
import reconciliation_worker as rw
import recon_evidence as ev
from tests._fakes import FakeColl, FakeDB, matches

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


# --- policy -----------------------------------------------------------------------------

# Copies of what the services accept. If either service's table changes, this test is the
# reminder to revisit policy._ALLOWED: transactions payments_service._LEGAL, plus the ledger
# resolution_service gates (RECHECK on MISSING/DISCREPANCY, LINK on MISSING/ORPHANED).
_TRANSACTIONS_LEGAL = {
    policy.CATEGORY_DISCREPANCY: {policy.ACCEPT, policy.POST_ADJUSTMENT, policy.ESCALATE},
    policy.CATEGORY_MISSING: {policy.ESCALATE},
    policy.CATEGORY_ORPHANED: {policy.ESCALATE, policy.DISMISS},
}
_LEDGER_LEGAL = {
    policy.CATEGORY_DISCREPANCY: {policy.RECHECK},
    policy.CATEGORY_MISSING: {policy.RECHECK, policy.LINK},
    policy.CATEGORY_ORPHANED: {policy.LINK},
}


@pytest.mark.parametrize("category", policy.WATCHED_CATEGORIES)
@pytest.mark.parametrize("cause", policy.CAUSES)
@pytest.mark.parametrize("bearer", ["DEBT", "SHAR", None])
def test_every_permitted_action_is_one_a_service_accepts(category, cause, bearer):
    legal = _TRANSACTIONS_LEGAL[category] | _LEDGER_LEGAL[category]
    assert policy.allowed_actions(category, cause, bearer) <= legal


def test_charge_bearer_decides_the_books_for_a_fee():
    assert policy.allowed_actions(policy.CATEGORY_DISCREPANCY, policy.FEE, "DEBT") == {policy.POST_ADJUSTMENT}
    for bearer in ("SHAR", "CRED", "SLEV", None):
        assert policy.allowed_actions(policy.CATEGORY_DISCREPANCY, policy.FEE, bearer) == {policy.ACCEPT}


def _check(**over):
    kw = dict(category=policy.CATEGORY_DISCREPANCY, cause=policy.FEE, charge_bearer="DEBT",
              action=policy.POST_ADJUSTMENT, params={"amount": 25.0}, candidates=[],
              discrepancy_amount=25.0)
    kw.update(over)
    return policy.check_proposal(**kw)


def test_adjustment_must_equal_the_discrepancy():
    assert _check() is None
    assert "exactly" in _check(params={"amount": 24.0})
    assert "exactly" in _check(params={})


def test_link_must_name_a_returned_candidate():
    base = dict(category=policy.CATEGORY_MISSING, cause=policy.REFERENCE_MISMATCH,
                charge_bearer="SHAR", action=policy.LINK)
    assert _check(**base, params={"candidateIndex": 0}, candidates=[{"lineNo": 3}]) is None
    assert "candidate" in _check(**base, params={"candidateIndex": 0}, candidates=[])
    assert "candidate" in _check(**base, params={"candidateIndex": 5}, candidates=[{"lineNo": 3}])


def test_no_proposal_before_a_cause_is_recorded():
    assert "cause" in _check(cause=None)


def test_recheck_minutes_are_capped():
    assert policy.capped_recheck_minutes(999) == policy.MAX_RECHECK_MINUTES
    assert policy.capped_recheck_minutes(0) == 1
    assert policy.capped_recheck_minutes("x") == policy.MAX_RECHECK_MINUTES


# --- candidate scoring ------------------------------------------------------------------

def test_reference_scoring_recognises_the_correspondents_rekey():
    # camt053.altered_reference: PAY-2452e147 -> 2452E147/LEAFYBK
    assert ev.reference_score("PAY-2452e147", "PAY-2452e147")[0] == ev.SCORE_EXACT
    assert ev.reference_score("2452E147/LEAFYBK", "PAY-2452e147")[0] == ev.SCORE_PREFIX
    assert ev.reference_score("ORPH-0A1B2C3D", "PAY-2452e147")[0] == ev.SCORE_AMOUNT_ONLY


def test_candidates_rank_by_reference_then_amount_closeness():
    ranked = ev.rank_candidates([
        {"reference": "ORPH-1", "paymentId": "PAY-aa", "amount": 100.0, "expectedAmount": 100.0},
        {"reference": "AA/LEAFYBK", "paymentId": "PAY-aa", "amount": 99.0, "expectedAmount": 100.0},
    ])
    assert ranked[0]["reference"] == "AA/LEAFYBK"
    assert ranked[0]["amountDelta"] == -1.0


# --- graph harness ----------------------------------------------------------------------

class _Script(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c-{name}"}])


def _db(exc: dict, payment: dict | None = None) -> FakeDB:
    db = FakeDB()
    db["exceptions"] = FakeColl([{"agent": None, "status": "OPEN", "createdAt": NOW, **exc}])
    db["payments"] = FakeColl([payment] if payment else [])
    return db


def _agent(db, *responses):
    return ra.build_reconciliation_agent(_Script(responses=list(responses)), db, InMemorySaver())


class _CallLog(list):
    fake = None


@pytest.fixture
def calls(monkeypatch):
    log = _CallLog()

    def fake(name, result):
        def _f(*a, **k):
            log.append((name, a, k))
            if isinstance(result, Exception):
                raise result
            return result(*a, **k) if callable(result) else result
        return _f

    log.fake = lambda name, result: monkeypatch.setattr(clients, name, fake(name, result))
    return log


def _agent_doc(db, exc_id):
    return db["exceptions"].find_one({"exceptionId": exc_id})["agent"]


# --- R1: DEBT fee → POST_ADJUSTMENT, approved, verified -----------------------------------

_R1 = {"exceptionId": "EXC-R1", "paymentId": "PAY-R1", "category": policy.CATEGORY_DISCREPANCY,
       "detail": {"discrepancyAmount": 25.0, "expectedAmount": 4850.0, "actualAmount": 4825.0}}


def test_fee_on_a_debt_wire_is_proposed_executed_and_verified(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})

    def resolve(exc_id, action, note=None):
        db["exceptions"].update_one({"exceptionId": exc_id}, {"$set": {"status": "RESOLVED"}})
        return {"status": "RESOLVED"}
    calls.fake("resolve", resolve)
    calls.fake("reconcile", {"outcome": "RECONCILED"})

    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="correspondent charge",
                         evidence=["line 4825.00 vs position 4850.00"], confidence="HIGH"),
                   _call("propose_action", action="POST_ADJUSTMENT", rationale="DEBT wire", amount=25.0),
                   AIMessage(content="Proposed the 5214 adjustment."))
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")

    doc = _agent_doc(db, "EXC-R1")
    assert doc["cause"] == "FEE"
    assert doc["proposedAction"]["action"] == "POST_ADJUSTMENT"
    assert ra.is_awaiting_approval(agent, "EXC-R1")
    assert not calls  # nothing executed before approval

    ra.resume(agent, db, "EXC-R1", ra.APPROVE)
    assert [c[0] for c in calls] == ["resolve"]
    assert calls[0][1][1] == "POST_ADJUSTMENT"
    doc = _agent_doc(db, "EXC-R1")
    assert doc["verification"]["result"] == ra.VERIFIED_RESOLVED
    assert doc["approval"]["decision"] == ra.APPROVE
    assert doc["proposedAction"] is None
    assert not ra.is_awaiting_approval(agent, "EXC-R1")


def test_the_agent_cannot_choose_accept_on_a_debt_wire(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})
    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="x", evidence=[], confidence="HIGH"),
                   _call("propose_action", action="ACCEPT_DISCREPANCY", rationale="cheaper"),
                   AIMessage(content="Could not propose."))
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    assert not _agent_doc(db, "EXC-R1").get("proposedAction")
    assert not ra.is_awaiting_approval(agent, "EXC-R1")


def test_reject_records_the_decision_and_executes_nothing(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})
    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="x", evidence=[], confidence="HIGH"),
                   _call("propose_action", action="POST_ADJUSTMENT", rationale="DEBT", amount=25.0),
                   AIMessage(content="done"))
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    ra.resume(agent, db, "EXC-R1", ra.REJECT, note="wait for the correspondent")
    assert not calls
    assert _agent_doc(db, "EXC-R1")["approval"]["decision"] == ra.REJECT


def test_a_service_refusal_is_recorded_then_reinvestigated_once(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})
    calls.fake("resolve", clients.ServiceRefused(409, "another resolver acted"))
    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="x", evidence=[], confidence="HIGH"),
                   _call("propose_action", action="POST_ADJUSTMENT", rationale="DEBT", amount=25.0),
                   AIMessage(content="proposed"),
                   AIMessage(content="nothing further"))  # the re-investigation turn
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    ra.resume(agent, db, "EXC-R1", ra.APPROVE)
    doc = _agent_doc(db, "EXC-R1")
    assert doc["actionsTaken"][-1]["result"] == ra.VERIFIED_REFUSED
    assert doc["verification"]["result"] == ra.VERIFIED_REFUSED


def test_execute_rechecks_policy_if_the_exception_moved_while_paused(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})
    calls.fake("resolve", {"status": "RESOLVED"})
    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="x", evidence=[], confidence="HIGH"),
                   _call("propose_action", action="POST_ADJUSTMENT", rationale="DEBT", amount=25.0),
                   AIMessage(content="proposed"), AIMessage(content="closed elsewhere"))
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    db["exceptions"].update_one({"exceptionId": "EXC-R1"}, {"$set": {"status": "RESOLVED"}})
    ra.resume(agent, db, "EXC-R1", ra.APPROVE)
    assert not calls  # never called the route on a closed exception


# --- R2: re-keyed reference → LINK to a returned candidate ------------------------------

_R2 = {"exceptionId": "EXC-R2", "paymentId": "PAY-2452e147", "category": policy.CATEGORY_MISSING,
       "detail": {"expectedAmount": 6120.0}}


def test_missing_with_a_rekeyed_line_links_the_candidate(calls, monkeypatch):
    db = _db(_R2, {"paymentId": "PAY-2452e147", "chargeBearer": "SHAR"})
    monkeypatch.setattr(ev, "find_candidates", lambda _db, _exc: ev.rank_candidates([{
        "paymentId": "PAY-2452e147", "paymentMessageId": "PM-2452e166", "lineNo": 1,
        "reference": "2452E147/LEAFYBK", "amount": 6120.0, "expectedAmount": 6120.0}]))

    def link(exc_id, **kw):
        db["exceptions"].update_one({"exceptionId": exc_id}, {"$set": {"status": "RESOLVED"}})
        return {"outcome": "RECONCILED"}
    calls.fake("link", link)
    calls.fake("reconcile", {"outcome": "RECONCILED"})

    agent = _agent(db,
                   _call("find_statement_candidates"),
                   _call("record_investigation", cause="REFERENCE_MISMATCH", root_cause="re-keyed",
                         evidence=["2452E147/LEAFYBK"], confidence="HIGH"),
                   _call("propose_action", action="LINK_STATEMENT_ENTRY", rationale="same amount",
                         candidate_index=0),
                   AIMessage(content="Proposed link."))
    ra.investigate(agent, db, "EXC-R2", "PAY-2452e147")
    ra.resume(agent, db, "EXC-R2", ra.APPROVE)

    name, _, kw = calls[0]
    assert name == "link"
    assert kw["payment_message_id"] == "PM-2452e166" and kw["line_no"] == 1
    assert _agent_doc(db, "EXC-R2")["verification"]["result"] == ra.VERIFIED_RESOLVED


def test_orphan_link_sends_the_payment_not_the_line(calls):
    exc = {"exceptionId": "EXC-O", "paymentId": "PM-1#2", "category": policy.CATEGORY_ORPHANED,
           "subjectRef": {"kind": "STATEMENT_LINE", "paymentMessageId": "PM-1", "lineNo": 2}}
    calls.fake("link", {"outcome": "RECONCILED"})
    ra.execute_proposal(FakeDB(), exc, {"action": policy.LINK,
                                        "params": {"target": {"paymentId": "PAY-x", "lineNo": 2}}})
    assert calls[0][2]["payment_id"] == "PAY-x"
    assert "line_no" not in calls[0][2]


# --- timing lag: autonomous recheck, no approval ----------------------------------------

def test_timing_lag_rechecks_and_schedules_without_approval(calls):
    db = _db({**_R2, "exceptionId": "EXC-L"}, {"paymentId": "PAY-2452e147", "chargeBearer": "SHAR"})
    calls.fake("recheck", {"outcome": "MISSING", "exception": {"status": "OPEN"}})
    agent = _agent(db,
                   _call("record_investigation", cause="TIMING_LAG", root_cause="late booking",
                         evidence=["p90 lag 90s"], confidence="MEDIUM"),
                   _call("recheck_reconciliation"),
                   _call("schedule_recheck", minutes=60),
                   AIMessage(content="Waiting for the next statement."))
    ra.investigate(agent, db, "EXC-L", "PAY-2452e147")
    doc = _agent_doc(db, "EXC-L")
    assert [c[0] for c in calls] == ["recheck"]
    assert doc["recheckCount"] == 1
    assert doc["nextCheckAt"] <= datetime.now(timezone.utc) + timedelta(minutes=policy.MAX_RECHECK_MINUTES)
    assert not ra.is_awaiting_approval(agent, "EXC-L")


def test_rechecks_stop_at_the_cap(calls):
    db = _db({**_R2, "exceptionId": "EXC-C"})
    db["exceptions"].update_one({"exceptionId": "EXC-C"}, {"$set": {"agent": {
        "cause": "TIMING_LAG", "recheckCount": policy.MAX_RECHECKS}}})
    tool = next(t for t in ra._build_tools(db) if t.name == "recheck_reconciliation")
    out = tool.func(state={"exception_id": "EXC-C", "payment_id": "PAY-2452e147"})
    assert "exhausted" in out and not calls


def test_a_paused_proposal_is_not_reinvestigated(calls):
    db = _db(_R1, {"paymentId": "PAY-R1", "chargeBearer": "DEBT"})
    agent = _agent(db,
                   _call("record_investigation", cause="FEE", root_cause="x", evidence=[], confidence="HIGH"),
                   _call("propose_action", action="POST_ADJUSTMENT", rationale="DEBT", amount=25.0),
                   AIMessage(content="proposed"))
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    before = _agent_doc(db, "EXC-R1")["proposedAction"]
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")  # the sweep firing again
    assert _agent_doc(db, "EXC-R1")["proposedAction"] == before


def test_investigate_never_raises():
    class _Boom:
        def invoke(self, *a, **k):
            raise RuntimeError("bedrock down")

        def get_state(self, cfg):
            raise RuntimeError("no state")
    db = _db(_R1)
    assert ra.investigate(_Boom(), db, "EXC-R1", "PAY-R1") is None


def test_the_system_prompt_reaches_the_model():
    seen = []

    class _Spy(_Script):
        def invoke(self, messages, *a, **k):
            seen.append(messages)
            return super().invoke(messages, *a, **k)

    db = _db(_R1)
    agent = ra.build_reconciliation_agent(_Spy(responses=[AIMessage(content="ok")]), db, InMemorySaver())
    ra.investigate(agent, db, "EXC-R1", "PAY-R1")
    assert seen[0][0].content == ra.RECONCILIATION_SYSTEM_PROMPT


# --- worker -----------------------------------------------------------------------------

def test_stream_filter_watches_all_three_categories_and_skips_precedents():
    m = rw._MATCH[0]["$match"]
    assert set(m["fullDocument.category"]["$in"]) == set(policy.WATCHED_CATEGORIES)
    live = {"historical": None}
    assert matches(live, {"historical": m["fullDocument.historical"]})
    assert not matches({"historical": {"correspondentBic": "BARCGB22"}},
                       {"historical": m["fullDocument.historical"]})


def test_sweep_picks_due_rechecks_and_unseen_exceptions_only():
    q = rw.sweep_query(NOW)
    base = {"category": policy.CATEGORY_MISSING, "status": "OPEN"}
    due = {**base, "agent": {"nextCheckAt": NOW - timedelta(seconds=1)}}
    later = {**base, "agent": {"nextCheckAt": NOW + timedelta(minutes=5)}}
    unseen_old = {**base, "agent": None, "createdAt": NOW - timedelta(minutes=5)}
    unseen_new = {**base, "agent": None, "createdAt": NOW - timedelta(seconds=10)}
    precedent = {**due, "status": "RESOLVED", "historical": {"correspondentBic": "X"}}
    assert matches(due, q) and matches(unseen_old, q)
    assert not matches(later, q) and not matches(unseen_new, q) and not matches(precedent, q)


def test_the_same_exception_is_never_run_twice_at_once():
    assert rw._claim("EXC-1")
    assert not rw._claim("EXC-1")
    rw._release("EXC-1")
    assert rw._claim("EXC-1")
    rw._release("EXC-1")


# --- reachability: each demo scenario has the action its beat needs ----------------------

@pytest.mark.parametrize("category,cause,bearer,needed", [
    (policy.CATEGORY_DISCREPANCY, policy.FEE, "DEBT", policy.POST_ADJUSTMENT),          # R1
    (policy.CATEGORY_DISCREPANCY, policy.FEE, "SHAR", policy.ACCEPT),                   # R1b
    (policy.CATEGORY_MISSING, policy.REFERENCE_MISMATCH, "SHAR", policy.LINK),          # R2
    (policy.CATEGORY_MISSING, policy.TIMING_LAG, "SHAR", policy.RECHECK),               # R3
    (policy.CATEGORY_DISCREPANCY, policy.AMOUNT_MISMATCH, "SHAR", policy.ESCALATE),     # R4
    (policy.CATEGORY_ORPHANED, policy.REFERENCE_MISMATCH, None, policy.LINK),           # R2 twin
    (policy.CATEGORY_ORPHANED, policy.ORPHANED, None, policy.ESCALATE),                 # R5
])
def test_every_demo_scenario_reaches_its_action(category, cause, bearer, needed):
    assert needed in policy.allowed_actions(category, cause, bearer)
