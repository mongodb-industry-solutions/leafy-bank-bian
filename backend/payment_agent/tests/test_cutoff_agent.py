"""Cutoff Agent (cutoff plan C2): case store, graph, tools, approval.

The LLM is a scripted fake that emits the tool calls a real run would; the transactions routes
are patched at `transactions_client`; stage timing is pinned per status so each scenario lands
on the risk level of the plan's scenario arithmetic. What is pinned: every scenario path, the
policy at the tool AND execute boundaries, blocker grounding, injected ids, and that the
agent never writes `payments` itself.
"""

from __future__ import annotations

import math
from time import sleep
from types import SimpleNamespace
from urllib.error import URLError
from datetime import date, datetime, time, timedelta, timezone

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pymongo.errors import DuplicateKeyError

import clients
import cutoff_agent as ca
import cutoff_cases as cases
import cutoff_evidence as ev
import cutoff_risk as cr
import cutoff_rules as rules
import cutoff_window as cw
import transactions_client
from tests._fakes import FakeColl, FakeDB

RUN = "RUN-C"
PID = "PAY-cut1"
DAY = date(2026, 10, 6)
VALUE_DATE = DAY.isoformat()

# Per-status timing (minutes) that reproduces the plan's scenario arithmetic.
TIMING = {
    cr.PENDING_APPROVAL: dict(stageP50Min=5, stageP90Min=10, remainingP50Min=8, remainingP90Min=20),
    cr.PENDING_FUNDS: dict(stageP50Min=5, stageP90Min=10, remainingP50Min=8, remainingP90Min=20),
    cr.PENDING_SCREENING: dict(stageP50Min=8, stageP90Min=16, remainingP50Min=5, remainingP90Min=10),
    cr.CUTOFF_EXCEPTION: dict(stageP50Min=1, stageP90Min=2, remainingP50Min=6, remainingP90Min=12),
}


def et(h, m):
    return datetime.combine(DAY, time(h, m), tzinfo=cw.ET).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def pinned_timing(monkeypatch):
    monkeypatch.setattr(ev, "stage_timing", lambda db, *, rail, status, at: dict(TIMING[status]))


def set_clock(db, at):
    """A run whose business time is `at` now (a second ahead, so a minute boundary holds)."""
    offset = math.ceil((at - datetime.now(timezone.utc)).total_seconds()) + 1
    db["demoClocks"] = FakeColl([{"_id": RUN, "offsetSeconds": offset,
                                  "businessDate": VALUE_DATE}])


def _staff():
    shift = {"startEt": "08:00", "endEt": "20:00"}
    return [
        {"staffId": "STF-maya", "name": "Maya", "role": "APPROVER", "primaryFor": ["ACC-1"],
         "shift": shift, "outOfOfficeUntil": et(20, 0)},
        {"staffId": "STF-raj", "name": "Raj", "role": "APPROVER", "backupFor": "STF-maya",
         "shift": shift},
        {"staffId": "STF-ana", "role": "ANALYST", "shift": shift},
        {"staffId": "STF-bo", "role": "ANALYST", "shift": shift, "outOfOfficeUntil": et(20, 0)},
        {"staffId": "STF-cy", "role": "ANALYST", "shift": shift, "outOfOfficeUntil": et(20, 0)},
    ]


def _world(status, wire_type, at, *, entered=None, amount=10_000.0, tagged=True):
    db = FakeDB()
    db["payments"] = FakeColl([{
        "paymentId": PID, "status": status, "rail": "WIRE",
        "wireDetails": {"wireType": wire_type}, "instructedCurrency": "USD",
        "instructedAmount": amount, "debtor": {"accountId": "ACC-1"}, "customerId": "CUST-1",
        "demo": {"clockRunId": RUN, "scenario": "C"} if tagged else None,
        "lifecycle": {"stateEnteredAt": entered or at, "events": []}, "cutoff": {},
    }])
    db["staffDirectory"] = FakeColl(_staff())
    db["cutoffCases"] = FakeColl(unique=[("caseId",),
                                         (("paymentId", "valueDate"), {"active": True})])
    set_clock(db, at)
    return db


def _approval_request(db, at):
    db["approvalRequests"] = FakeColl([{
        "approvalRequestId": "APR-1", "paymentId": PID, "status": "OPEN",
        "primaryApprover": "STF-maya", "assignedTo": "STF-maya", "requestedAt": at,
        "reminders": [], "escalatedTo": None}])


def _screening_queue(db, at, *, ahead):
    db["screeningQueue"] = FakeColl(
        [{"screeningItemId": f"SQ-syn{i}", "paymentId": f"SYN-{i}", "status": "OPEN",
          "priority": 0, "demo": {"clockRunId": RUN},
          "queuedAt": at - timedelta(minutes=30 - i)} for i in range(ahead)]
        + [{"screeningItemId": "SQ-1", "paymentId": PID, "status": "OPEN", "priority": 0,
            "demo": {"clockRunId": RUN}, "queuedAt": at - timedelta(minutes=1)}])


def _open_case(db, at):
    payment = db["payments"].find_one({"paymentId": PID})
    return cases.open_or_get(db, payment=payment, risk=None, value_date=VALUE_DATE,
                             business_at=at)["caseId"]


def _move(db, at, **fields):
    set_clock(db, at)
    if fields:
        db["payments"].update_one({"paymentId": PID}, {"$set": fields})


class _Script(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c-{name}"}])


def _done(text="Summary for the operator."):
    return AIMessage(content=text)


def _assess(blocker, kind, action=None):
    return _call("record_assessment", diagnosis="d", blocker=blocker, evidence=["e"],
                 confidence="HIGH", recommendation_kind=kind, recommendation_action=action,
                 rationale="r")


def _agent(db, *responses):
    return ca.build_cutoff_agent(_Script(responses=list(responses)), db, InMemorySaver())


def _case(db, case_id):
    return cases.get(db, case_id)


def _tool_replies(agent, case_id, name):
    return [m.content for m in ca.thread_messages(agent, case_id)
            if getattr(m, "type", None) == "tool" and m.name == name]


class _RouteLog(list):
    install = None


@pytest.fixture
def routes(monkeypatch):
    """Patched transactions routes that do what the real ones do to the fake payment."""
    log = _RouteLog()

    def install(db, *, refuse=None):
        def hold(payment_id, *, decided_by, reason):
            log.append(("hold", payment_id, decided_by, reason))
            db["payments"].update_one({"paymentId": payment_id}, {"$set": {
                "cutoff.decision": "HOLD_NEXT_VALUE_DATE", "cutoff.valueDate": "2026-10-07"}})
            return {"status": "PENDING_FUNDS"}

        def decide(payment_id, *, decision, decided_by):
            log.append(("decide", payment_id, decision, decided_by))
            if refuse:
                raise clients.ServiceRefused(400, refuse)
            if decision == "EXPEDITE":
                at = ca.cutoff_clock.now(db, RUN)
                update = {"status": "SUBMITTED", "cutoff.decision": "EXPEDITE",
                          "lifecycle.events": [{"state": "SUBMITTED", "at": at}]}
            else:
                update = {"status": "ROUTED", "cutoff.decision": "NEXT_VALUE_DATE",
                          "cutoff.valueDate": "2026-10-07"}
            db["payments"].update_one({"paymentId": payment_id}, {"$set": update})
            return {"status": update["status"]}
        monkeypatch.setattr(transactions_client, "hold_next_value_date", hold)
        monkeypatch.setattr(transactions_client, "cutoff_decision", decide)
    log.install = install
    return log


# --- C1: approval → remind + escalate; after 18:15 → EXPEDITE --------------------------------

def test_c1_p1_reminds_escalates_and_hold_is_refused(routes):
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    routes.install(db)
    case_id = _open_case(db, et(17, 5))
    agent = _agent(db,
                   _call("get_cutoff_window"),
                   _call("send_approval_reminder"),
                   _call("escalate_to_backup_approver"),
                   _assess("APPROVAL", "AUTONOMOUS", rules.ESCALATE_TO_BACKUP_APPROVER),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _done())
    ca.investigate(agent, db, case_id, PID)

    req = db["approvalRequests"].find_one({"paymentId": PID})
    assert len(req["reminders"]) == 1 and req["reminders"][0]["to"] == "STF-maya"
    assert req["escalatedTo"] == "STF-raj" and req["assignedTo"] == "STF-raj"
    case = _case(db, case_id)
    assert case["risk"]["riskLevel"] == cr.AT_RISK and case["risk"]["phase"] == cw.BEFORE_INTERNAL
    assert "needs risk WILL_MISS" in _tool_replies(agent, case_id, "propose_resolution")[0]
    assert case["agent"]["proposedAction"] is None and case["status"] == cases.OPEN
    assert not ca.is_awaiting_approval(agent, case_id) and not routes


def test_c1_p2_same_case_expedites_after_internal_and_verifies(routes):
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    routes.install(db)
    case_id = _open_case(db, et(17, 5))
    agent = _agent(db,
                   _assess("APPROVAL", "AUTONOMOUS", rules.SEND_APPROVAL_REMINDER), _done(),
                   _call("get_cutoff_window"),
                   _assess("CUTOFF_DECISION", "NEEDS_APPROVAL", rules.EXPEDITE),
                   _call("propose_resolution", action="EXPEDITE", rationale="p90 fits"),
                   _done())
    ca.investigate(agent, db, case_id, PID)

    # Approved at 18:10, past the domestic internal cut-off → CUTOFF_EXCEPTION; agent at 18:16.
    _move(db, et(18, 16), status=cr.CUTOFF_EXCEPTION, **{"lifecycle.stateEnteredAt": et(18, 10)})
    assert cases.open_or_get(db, payment=db["payments"].find_one({"paymentId": PID}), risk=None,
                             value_date=VALUE_DATE, business_at=et(18, 16))["caseId"] == case_id
    ca.investigate(agent, db, case_id, PID)
    assert ca.is_awaiting_approval(agent, case_id)
    assert _case(db, case_id)["status"] == cases.AWAITING_APPROVAL
    assert not routes  # nothing executed before approval

    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")
    assert routes == [("decide", PID, "EXPEDITE", "kiran")]
    case = _case(db, case_id)
    assert case["outcome"]["result"] == cases.SUBMITTED_IN_TIME
    assert case["status"] == cases.ACTIONED and case["active"] is False
    assert case["agent"]["verification"]["result"] == ca.VERIFIED


# --- C2: screening → raise priority; after 18:15 → resolve blocker, then EXPEDITE ------------

def test_c2_p1_priority_must_be_raised_before_a_hold(routes):
    db = _world(cr.PENDING_SCREENING, "INTERNATIONAL", et(17, 50))
    _screening_queue(db, et(17, 50), ahead=7)
    routes.install(db)
    case_id = _open_case(db, et(17, 50))
    agent = _agent(db,
                   _assess("SCREENING", "NEEDS_APPROVAL", rules.HOLD_NEXT_VALUE_DATE),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _call("raise_screening_priority"),
                   _assess("SCREENING", "AUTONOMOUS", rules.RAISE_SCREENING_PRIORITY),
                   _done())
    ca.investigate(agent, db, case_id, PID)

    assert "RAISE_SCREENING_PRIORITY" in _tool_replies(agent, case_id, "propose_resolution")[0]
    assert db["screeningQueue"].find_one({"paymentId": PID})["priority"] == 1
    assert "position 1" in _tool_replies(agent, case_id, "raise_screening_priority")[0]
    levels = [r["riskLevel"] for r in _case(db, case_id)["riskHistory"]]
    assert levels[-2:] == [cr.WILL_MISS, cr.AT_RISK]
    assert not ca.is_awaiting_approval(agent, case_id)


def test_c2_p2_resolve_blocker_then_expedite_after_clear(routes):
    db = _world(cr.PENDING_SCREENING, "INTERNATIONAL", et(18, 16), entered=et(17, 50))
    _screening_queue(db, et(18, 16), ahead=0)
    routes.install(db)
    case_id = _open_case(db, et(18, 16))
    agent = _agent(db,
                   _assess("SCREENING", "RESOLVE_BLOCKER"),
                   _call("propose_resolution", action="EXPEDITE", rationale="x"),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _done(),
                   _assess("CUTOFF_DECISION", "NEEDS_APPROVAL", rules.EXPEDITE),
                   _call("propose_resolution", action="EXPEDITE", rationale="cleared"),
                   _done())
    ca.investigate(agent, db, case_id, PID)
    case = _case(db, case_id)
    assert case["risk"]["phase"] == cw.AFTER_INTERNAL and case["risk"]["riskLevel"] == cr.AT_RISK
    assert case["agent"]["recommendation"] == {"action": None, "kind": "RESOLVE_BLOCKER",
                                               "rationale": "r"}
    replies = _tool_replies(agent, case_id, "propose_resolution")
    assert "CUTOFF_EXCEPTION" in replies[0] and "WILL_MISS" in replies[1]

    # The presenter clears screening after the internal cut-off → CUTOFF_EXCEPTION.
    _move(db, et(18, 17), status=cr.CUTOFF_EXCEPTION, **{"lifecycle.stateEnteredAt": et(18, 17)})
    ca.investigate(agent, db, case_id, PID)
    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")
    assert _case(db, case_id)["outcome"]["result"] == cases.SUBMITTED_IN_TIME


# --- C3: funds → notify, then HOLD_NEXT_VALUE_DATE --------------------------------------------

def _funds_world():
    db = _world(cr.PENDING_FUNDS, "DOMESTIC", et(16, 45), amount=8_200.0)
    db["accounts"] = FakeColl([{"accountId": "ACC-1", "balance": {"available": 5_000.0},
                                "expectedCredits": [{"amount": 3_200.0, "expectedAtEt": "17:45",
                                                     "status": "EXPECTED"}]}])
    return db


def test_c3_notifies_then_holds_for_next_value_date(routes):
    db = _funds_world()
    routes.install(db)
    case_id = _open_case(db, et(16, 45))
    agent = _agent(db,
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _assess("FUNDS", "NEEDS_APPROVAL", rules.HOLD_NEXT_VALUE_DATE),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _call("notify_customer_funds"),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE",
                         rationale="credit lands 17:45, after the 17:30 cut-off"),
                   _done())
    ca.investigate(agent, db, case_id, PID)

    replies = _tool_replies(agent, case_id, "propose_resolution")
    assert "record_assessment first" in replies[0]
    assert "NOTIFY_CUSTOMER_FUNDS" in replies[1]
    assert replies[2].startswith("Proposed HOLD_NEXT_VALUE_DATE")
    notif = db["notifications"].find_one({"paymentId": PID})
    assert notif["sourceSystem"] == "cutoff-agent" and notif["caseId"] == case_id
    assert "3200.0" in notif["message"]
    assert _case(db, case_id)["risk"]["riskLevel"] == cr.WILL_MISS

    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")
    (_, pid, by, reason), = routes
    assert (pid, by) == (PID, "kiran") and case_id in reason
    case = _case(db, case_id)
    assert case["outcome"]["result"] == cases.HELD_NEXT_VALUE_DATE
    assert case["outcome"]["valueDate"] == "2026-10-07"
    # The agent never wrote the payment's status: the hold route records a decision only.
    assert db["payments"].find_one({"paymentId": PID})["status"] == cr.PENDING_FUNDS


# --- C4: on track → assessment only ----------------------------------------------------------

def test_c4_on_track_records_an_assessment_and_nothing_else(routes):
    db = _world(cr.PENDING_SCREENING, "INTERNATIONAL", et(16, 34), entered=et(16, 30))
    _screening_queue(db, et(16, 34), ahead=1)
    db["staffDirectory"].update_many({"role": "ANALYST"}, {"$set": {"outOfOfficeUntil": None}})
    routes.install(db)
    case_id = _open_case(db, et(16, 34))
    agent = _agent(db,
                   _call("raise_screening_priority"),
                   _assess("SCREENING", "AUTONOMOUS", rules.RAISE_SCREENING_PRIORITY),
                   _assess("SCREENING", "NONE"),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _done())
    ca.investigate(agent, db, case_id, PID)

    assert "Refused" in _tool_replies(agent, case_id, "raise_screening_priority")[0]
    first, second = _tool_replies(agent, case_id, "record_assessment")
    assert "must be NONE" in first and second.startswith("Recorded (risk ON_TRACK")
    case = _case(db, case_id)
    assert case["agent"]["recommendation"]["kind"] == "NONE"
    assert case["status"] == cases.OPEN and case["active"] is True
    assert db["screeningQueue"].find_one({"paymentId": PID})["priority"] == 0
    assert not routes and not ca.is_awaiting_approval(agent, case_id)


# --- C5: ten minutes to Fedwire → only DEFER --------------------------------------------------

@pytest.mark.parametrize("minute,phase", [(35, cw.AFTER_INTERNAL), (50, cw.AFTER_EXTERNAL)])
def test_c5_only_defer_is_allowed(routes, minute, phase):
    db = _world(cr.CUTOFF_EXCEPTION, "INTERNATIONAL", et(18, minute), entered=et(18, 20))
    routes.install(db)
    case_id = _open_case(db, et(18, minute))
    agent = _agent(db,
                   _assess("CUTOFF_DECISION", "NEEDS_APPROVAL", rules.DEFER_NEXT_BUSINESS_DAY),
                   _call("propose_resolution", action="EXPEDITE", rationale="x"),
                   _call("propose_resolution", action="DEFER_NEXT_BUSINESS_DAY", rationale="late"),
                   _done())
    ca.investigate(agent, db, case_id, PID)
    case = _case(db, case_id)
    assert (case["risk"]["phase"], case["risk"]["riskLevel"]) == (phase, cr.WILL_MISS)
    assert "Refused" in _tool_replies(agent, case_id, "propose_resolution")[0]

    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")
    assert routes == [("decide", PID, "NEXT_VALUE_DATE", "kiran")]
    assert _case(db, case_id)["outcome"]["result"] == cases.DEFERRED_NEXT_BUSINESS_DAY


# --- approval edge cases ---------------------------------------------------------------------

def _paused_c5(routes, *more):
    db = _world(cr.CUTOFF_EXCEPTION, "INTERNATIONAL", et(18, 35), entered=et(18, 20))
    routes.install(db)
    case_id = _open_case(db, et(18, 35))
    agent = _agent(db,
                   _assess("CUTOFF_DECISION", "NEEDS_APPROVAL", rules.DEFER_NEXT_BUSINESS_DAY),
                   _call("propose_resolution", action="DEFER_NEXT_BUSINESS_DAY", rationale="x"),
                   _done(), *more)
    ca.investigate(agent, db, case_id, PID)
    assert ca.is_awaiting_approval(agent, case_id)
    return db, case_id, agent


def test_reject_records_it_and_the_same_action_cannot_be_reproposed_in_phase(routes):
    db, case_id, agent = _paused_c5(
        routes, _call("propose_resolution", action="DEFER_NEXT_BUSINESS_DAY", rationale="again"),
        _done())
    ca.resume(agent, db, case_id, ca.REJECT, note="call the customer first", by="kiran")
    case = _case(db, case_id)
    assert case["agent"]["approval"]["decision"] == ca.REJECT
    assert case["agent"]["rejectedActions"][0]["action"] == rules.DEFER_NEXT_BUSINESS_DAY
    assert case["agent"]["rejectedActions"][0]["phase"] == cw.AFTER_INTERNAL
    assert case["status"] == cases.OPEN and not routes

    ca.investigate(agent, db, case_id, PID)
    assert "rejected in phase" in _tool_replies(agent, case_id, "propose_resolution")[-1]
    assert not ca.is_awaiting_approval(agent, case_id)


def _paused_expedite(routes, *more):
    db = _world(cr.CUTOFF_EXCEPTION, "DOMESTIC", et(18, 16), entered=et(18, 10))
    routes.install(db)
    case_id = _open_case(db, et(18, 16))
    agent = _agent(db,
                   _assess("CUTOFF_DECISION", "NEEDS_APPROVAL", rules.EXPEDITE),
                   _call("propose_resolution", action="EXPEDITE", rationale="fits"),
                   _done(), *more)
    ca.investigate(agent, db, case_id, PID)
    assert ca.is_awaiting_approval(agent, case_id)
    return db, case_id, agent


_REPROPOSE = (_call("propose_resolution", action="EXPEDITE", rationale="retry"),
              _call("propose_resolution", action="DEFER_NEXT_BUSINESS_DAY", rationale="late"),
              _done())


def test_clock_passing_the_external_cutoff_while_paused_refuses_expedite_at_execute(routes):
    db, case_id, agent = _paused_expedite(routes, *_REPROPOSE)
    set_clock(db, et(18, 50))
    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")

    assert not routes  # the route was never called
    taken = _case(db, case_id)["agent"]["actionsTaken"]
    assert {"action": "EXPEDITE", "result": "REFUSED"}.items() <= taken[-1].items()
    assert "AFTER_EXTERNAL" in taken[-1]["detail"]
    assert "Refused" in _tool_replies(agent, case_id, "propose_resolution")[-2]
    # Re-investigated once, and re-paused on the one action still permitted.
    assert ca.is_awaiting_approval(agent, case_id)
    assert _case(db, case_id)["agent"]["proposedAction"]["action"] == rules.DEFER_NEXT_BUSINESS_DAY


def test_an_exception_opening_while_paused_refuses_expedite_at_execute(routes):
    db, case_id, agent = _paused_expedite(routes, *_REPROPOSE)
    db["exceptions"] = FakeColl([{"exceptionId": "EXC-1", "paymentId": PID, "status": "OPEN",
                                  "category": "SANCTIONS_REVIEW"}])
    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")

    assert not routes
    assert "open exception" in _case(db, case_id)["agent"]["actionsTaken"][-1]["detail"]
    assert "open exception" in _tool_replies(agent, case_id, "propose_resolution")[-2]
    assert _case(db, case_id)["agent"]["proposedAction"]["action"] == rules.DEFER_NEXT_BUSINESS_DAY


def test_a_route_refusal_is_recorded_then_reinvestigated_once(routes):
    db, case_id, agent = _paused_c5(routes, _done("Nothing more to do."))
    routes.install(db, refuse="Payment has an OPEN exception.")
    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")
    case = _case(db, case_id)
    assert case["agent"]["verification"]["result"] == ca.VERIFIED_REFUSED
    assert case["agent"]["actionsTaken"][-1]["detail"] == "Payment has an OPEN exception."
    assert case["active"] is True and not ca.is_awaiting_approval(agent, case_id)


def test_supersede_closes_a_paused_case_and_frees_the_slot(routes):
    db, case_id, agent = _paused_c5(routes)
    ca.supersede(agent, db, case_id)
    case = _case(db, case_id)
    assert not ca.is_awaiting_approval(agent, case_id)
    assert case["agent"]["approval"]["decision"] == ca.SUPERSEDED
    assert case["outcome"]["result"] == cases.SUPERSEDED and case["active"] is False
    assert not routes
    fresh = cases.open_or_get(db, payment=db["payments"].find_one({"paymentId": PID}),
                              risk=None, value_date=VALUE_DATE, business_at=et(18, 36))
    assert fresh["caseId"] != case_id


# --- grounding and isolation ------------------------------------------------------------------

def test_a_blocker_that_differs_from_the_computed_one_is_refused(routes):
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    agent = _agent(db, _assess("APPROVAL", "NEEDS_APPROVAL", rules.HOLD_NEXT_VALUE_DATE), _done())
    ca.investigate(agent, db, case_id, PID)
    assert "computed blocker FUNDS" in _tool_replies(agent, case_id, "record_assessment")[0]
    assert "diagnosis" not in _case(db, case_id)["agent"]


def test_ids_come_from_graph_state_not_model_arguments(routes):
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    other = {"paymentId": "PAY-other", "caseId": "CUT-other", "active": True,
             "valueDate": VALUE_DATE, "agent": {}}
    db["cutoffCases"].insert_one(dict(other))
    agent = _agent(db,
                   _call("notify_customer_funds",
                         state={"case_id": "CUT-other", "payment_id": "PAY-other"},
                         case_id="CUT-other", payment_id="PAY-other"),
                   _done())
    ca.investigate(agent, db, case_id, PID)
    assert db["notifications"].find_one({})["paymentId"] == PID
    assert _case(db, case_id)["agent"]["actionsTaken"][-1]["action"] == rules.NOTIFY_CUSTOMER_FUNDS
    assert _case(db, "CUT-other")["agent"] == {}


def test_an_untagged_payment_is_never_acted_on(routes):
    db = _world(cr.PENDING_FUNDS, "DOMESTIC", et(16, 45), tagged=False)
    case_id = _open_case(db, et(16, 45))
    agent = _agent(db, _call("notify_customer_funds"),
                   _call("propose_resolution", action="HOLD_NEXT_VALUE_DATE", rationale="x"),
                   _done())
    ca.investigate(agent, db, case_id, PID)
    assert all("Refused" in r for r in _tool_replies(agent, case_id, "notify_customer_funds")
               + _tool_replies(agent, case_id, "propose_resolution"))
    assert db["notifications"].find_one({}) is None and not routes


def test_escalation_needs_the_backup_on_shift(routes):
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    db["staffDirectory"].update_one({"staffId": "STF-raj"},
                                    {"$set": {"outOfOfficeUntil": et(20, 0)}})
    case_id = _open_case(db, et(17, 5))
    agent = _agent(db, _call("escalate_to_backup_approver"), _done())
    ca.investigate(agent, db, case_id, PID)
    assert "not permitted" in _tool_replies(agent, case_id, "escalate_to_backup_approver")[0]
    assert db["approvalRequests"].find_one({"paymentId": PID})["escalatedTo"] is None


def test_reminders_stop_at_the_cap(routes):
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    case_id = _open_case(db, et(17, 5))
    agent = _agent(db, *[AIMessage(content="", tool_calls=[
        {"name": "send_approval_reminder", "args": {}, "id": f"c-rem{i}"}]) for i in range(3)],
        _done())
    ca.investigate(agent, db, case_id, PID)
    replies = _tool_replies(agent, case_id, "send_approval_reminder")
    assert [r.startswith("Reminder sent") for r in replies] == [True, True, False]
    assert len(db["approvalRequests"].find_one({"paymentId": PID})["reminders"]) == 2


# --- never-raise, pause detection, prompt -----------------------------------------------------

class _Boom(_Script):
    def invoke(self, *a, **k):
        raise RuntimeError("ExpiredTokenException")


def test_investigate_never_raises_and_records_the_error():
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    agent = ca.build_cutoff_agent(_Boom(responses=[_done()]), db, InMemorySaver())
    assert ca.investigate(agent, db, case_id, PID) is None
    assert ca.investigate(agent, db, case_id, PID) is None
    error = _case(db, case_id)["agent"]["error"]
    assert "ExpiredTokenException" in error["message"] and error["attempts"] == 2
    assert not ca.is_awaiting_approval(agent, case_id)


def test_a_paused_case_is_not_reinvestigated(routes):
    db, case_id, agent = _paused_c5(routes)
    before = len(ca.thread_messages(agent, case_id))
    ca.investigate(agent, db, case_id, PID)
    assert len(ca.thread_messages(agent, case_id)) == before
    assert ca.is_awaiting_approval(agent, case_id)


def test_the_system_prompt_and_steps_are_shared_with_recon(routes):
    seen = []

    class _Spy(_Script):
        def invoke(self, messages, *a, **k):
            seen.append(messages[0].content)
            return super().invoke(messages, *a, **k)
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    agent = ca.build_cutoff_agent(_Spy(responses=[_done()]), db, InMemorySaver())
    ca.investigate(agent, db, case_id, PID)
    assert seen[0] == ca.CUTOFF_SYSTEM_PROMPT
    kinds = [s["kind"] for s in ca.messages_to_steps(ca.thread_messages(agent, case_id))]
    assert kinds == ["note", "thought"]


# --- verify -----------------------------------------------------------------------------------

def test_expedite_verifies_only_when_submitted_by_the_external_cutoff():
    risk = {"externalCutoffAt": et(18, 45)}
    submitted = lambda at: {"status": "SUBMITTED", "lifecycle": {"events": [
        {"state": "SUBMITTED", "at": at}]}}
    assert ca.verify_outcome(submitted(et(18, 44)), rules.EXPEDITE, risk) == cases.SUBMITTED_IN_TIME
    assert ca.verify_outcome(submitted(et(18, 46)), rules.EXPEDITE, risk) is None
    assert ca.verify_outcome({"status": "MANUAL_FRAUD_REVIEW"}, rules.EXPEDITE, risk) is None
    assert ca.verify_outcome({"status": "ROUTED", "cutoff": {"decision": "EXPEDITE"}},
                             rules.DEFER_NEXT_BUSINESS_DAY, risk) is None


@pytest.mark.parametrize("status", ["ROUTED", "AUTHORISED", "SUBMITTED", "SETTLED"])
def test_defer_verifies_at_any_status_but_cutoff_exception(status):
    payment = {"status": status, "cutoff": {"decision": "NEXT_VALUE_DATE"}}
    assert ca.verify_outcome(payment, rules.DEFER_NEXT_BUSINESS_DAY, None) == cases.DEFERRED_NEXT_BUSINESS_DAY


def test_defer_does_not_verify_without_the_decision_or_while_excepted():
    assert ca.verify_outcome({"status": "CUTOFF_EXCEPTION", "cutoff": {"decision": "NEXT_VALUE_DATE"}},
                             rules.DEFER_NEXT_BUSINESS_DAY, None) is None
    assert ca.verify_outcome({"status": "SETTLED", "cutoff": {}}, rules.DEFER_NEXT_BUSINESS_DAY, None) is None


# --- the case store ---------------------------------------------------------------------------

def _store():
    db = FakeDB()
    db["cutoffCases"] = FakeColl(unique=[("caseId",),
                                         (("paymentId", "valueDate"), {"active": True})])
    return db


_PAYMENT = {"paymentId": PID, "status": cr.PENDING_FUNDS, "demo": {"clockRunId": RUN}}


def test_one_active_case_per_payment_and_value_date():
    db = _store()
    risk = {"phase": cw.BEFORE_INTERNAL, "riskLevel": cr.WILL_MISS, "blockerType": "FUNDS"}
    first = cases.open_or_get(db, payment=_PAYMENT, risk=risk, value_date=VALUE_DATE,
                              business_at=et(16, 45))
    again = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                              business_at=et(16, 50))
    assert again["caseId"] == first["caseId"] and first["caseId"].startswith("CUT-")
    assert first["trigger"]["key"] == "PENDING_FUNDS|BEFORE_INTERNAL|WILL_MISS|FUNDS"
    tomorrow = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date="2026-10-07",
                                 business_at=et(16, 50))
    assert tomorrow["caseId"] != first["caseId"]
    with pytest.raises(DuplicateKeyError):
        db["cutoffCases"].insert_one({"caseId": "CUT-x", "paymentId": PID,
                                      "valueDate": VALUE_DATE, "active": True})


def test_a_lost_insert_race_returns_the_winner(monkeypatch):
    db = _store()
    winner = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                               business_at=et(16, 45))
    monkeypatch.setattr(cases, "active_for", lambda *a, _n=iter([None]): next(_n, None) or
                        db["cutoffCases"].find_one({"caseId": winner["caseId"]}))
    assert cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                             business_at=et(16, 46))["caseId"] == winner["caseId"]


def test_lease_blocks_a_second_owner_until_it_expires():
    db = _store()
    case_id = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                                business_at=et(16, 45))["caseId"]
    t0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert cases.claim_lease(db, case_id, "a", now=t0)
    assert cases.claim_lease(db, case_id, "a", now=t0)
    assert not cases.claim_lease(db, case_id, "b", now=t0 + timedelta(seconds=119))
    assert cases.claim_lease(db, case_id, "b", now=t0 + timedelta(seconds=121))
    cases.release_lease(db, case_id, "b")
    assert cases.claim_lease(db, case_id, "a", now=t0 + timedelta(seconds=122))


@pytest.mark.parametrize("result,status", [
    (cases.SUBMITTED_IN_TIME, cases.ACTIONED), (cases.HELD_NEXT_VALUE_DATE, cases.ACTIONED),
    (cases.DEFERRED_NEXT_BUSINESS_DAY, cases.ACTIONED), (cases.SUPERSEDED, cases.RESOLVED),
    (cases.NO_ACTION_NEEDED, cases.RESOLVED), (cases.CLOCK_EXPIRED, cases.EXPIRED)])
def test_close_maps_the_outcome_to_a_status(result, status):
    db = _store()
    case_id = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                                business_at=et(16, 45))["caseId"]
    cases.close(db, case_id, result=result)
    case = cases.get(db, case_id)
    assert (case["status"], case["active"], case["outcome"]["result"]) == (status, False, result)


def test_risk_history_keeps_the_last_twenty():
    db = _store()
    case_id = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                                business_at=et(16, 45))["caseId"]
    for i in range(25):
        cases.record_risk(db, case_id, {"riskLevel": cr.AT_RISK, "i": i})
    history = cases.get(db, case_id)["riskHistory"]
    assert len(history) == 20 and history[0]["i"] == 5 and history[-1]["i"] == 24


# --- review fixes (review-cutoff-c.md) --------------------------------------------------------

@pytest.mark.parametrize("landed", [False, True])
def test_a_5xx_or_timeout_at_execute_is_recorded_and_verify_reads_the_payment(
        routes, monkeypatch, landed):
    db, case_id, agent = _paused_c5(routes, _done("Nothing more to do."))

    def decide(payment_id, *, decision, decided_by):
        if landed:  # the route committed, then the response was lost
            db["payments"].update_one({"paymentId": payment_id}, {"$set": {
                "status": "ROUTED", "cutoff.decision": "NEXT_VALUE_DATE"}})
            raise TimeoutError("read timed out")
        raise URLError("transactions 503")

    monkeypatch.setattr(transactions_client, "cutoff_decision", decide)
    ca.resume(agent, db, case_id, ca.APPROVE, by="kiran")  # must not raise
    case = _case(db, case_id)
    error = next(a for a in case["agent"]["actionsTaken"] if a["result"] == "ERROR")
    assert error["action"] == rules.DEFER_NEXT_BUSINESS_DAY
    assert case["agent"]["verification"]["executeError"]
    assert case["status"] != cases.AWAITING_APPROVAL
    assert not ca.is_awaiting_approval(agent, case_id)
    if landed:
        assert case["agent"]["verification"]["result"] == ca.VERIFIED
        assert case["outcome"]["result"] == cases.DEFERRED_NEXT_BUSINESS_DAY
    else:
        assert case["agent"]["verification"]["result"] == ca.VERIFIED_NOT_APPLIED
        assert case["status"] == cases.OPEN and case["active"] is True


def test_close_on_an_inactive_case_is_a_no_op():
    db = _store()
    case_id = cases.open_or_get(db, payment=_PAYMENT, risk=None, value_date=VALUE_DATE,
                                business_at=et(16, 45))["caseId"]
    assert cases.close(db, case_id, result=cases.RELEASED)
    assert not cases.close(db, case_id, result=cases.SUBMITTED_IN_TIME)
    assert not cases.set_fields(db, case_id, {"status": cases.OPEN}, only_active=True)
    case = cases.get(db, case_id)
    assert case["outcome"]["result"] == cases.RELEASED and case["status"] == cases.RESOLVED


def _slow_checks(monkeypatch):
    """Widen the check-to-record window so parallel tool calls overlap without the lock."""
    real = ca._auto_refusal

    def slow(*a):
        out = real(*a)
        sleep(0.2)
        return out

    monkeypatch.setattr(ca, "_auto_refusal", slow)


def _parallel(name, n):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": f"p-{name}{i}"}
                                             for i in range(n)])


def test_parallel_notify_calls_in_one_turn_notify_once(routes, monkeypatch):
    _slow_checks(monkeypatch)
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    agent = _agent(db, _parallel("notify_customer_funds", 2), _done())
    ca.investigate(agent, db, case_id, PID)
    assert db["notifications"].count_documents({}) == 1
    assert ca._taken(_case(db, case_id)) == {rules.NOTIFY_CUSTOMER_FUNDS: 1}


def test_parallel_reminders_in_one_turn_stop_at_the_cap(routes, monkeypatch):
    _slow_checks(monkeypatch)
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    case_id = _open_case(db, et(17, 5))
    agent = _agent(db, _parallel("send_approval_reminder", 3), _done())
    ca.investigate(agent, db, case_id, PID)
    assert len(db["approvalRequests"].find_one({"paymentId": PID})["reminders"]) == 2


def test_an_escalation_that_matches_nothing_is_refused_not_done(routes, monkeypatch):
    db = _world(cr.PENDING_APPROVAL, "DOMESTIC", et(17, 5))
    _approval_request(db, et(16, 50))
    case_id = _open_case(db, et(17, 5))
    # The request closes between the policy check and the write.
    monkeypatch.setattr(db["approvalRequests"], "update_one",
                        lambda *a, **k: SimpleNamespace(matched_count=0))
    agent = _agent(db, _call("escalate_to_backup_approver"), _done())
    ca.investigate(agent, db, case_id, PID)
    assert _tool_replies(agent, case_id, "escalate_to_backup_approver")[0].startswith("Refused")
    taken = _case(db, case_id)["agent"]["actionsTaken"]
    assert taken[-1]["result"] == ca.VERIFIED_REFUSED
    assert rules.ESCALATE_TO_BACKUP_APPROVER not in ca._taken(_case(db, case_id))


def test_supersede_stamps_business_time(routes):
    db, case_id, agent = _paused_c5(routes)
    ca.supersede(agent, db, case_id)
    at = _case(db, case_id)["outcome"]["at"]
    assert abs((at - et(18, 35)).total_seconds()) < 60


def test_investigate_never_raises_when_reading_the_case_fails(monkeypatch):
    db = _funds_world()
    case_id = _open_case(db, et(16, 45))
    agent = _agent(db, _done())
    real_get = cases.get
    calls = iter([RuntimeError("primary stepped down")])

    def flaky(db_, cid):
        err = next(calls, None)
        if err:
            raise err
        return real_get(db_, cid)

    monkeypatch.setattr(cases, "get", flaky)
    assert ca.investigate(agent, db, case_id, PID) is None
    assert "primary stepped down" in real_get(db, case_id)["agent"]["error"]["message"]


def test_fake_lease_filter_matches_a_missing_lease_like_mongodb():
    db = _store()
    db["cutoffCases"].insert_one({"caseId": "CUT-noagent"})
    db["cutoffCases"].insert_one({"caseId": "CUT-nolease", "agent": {}})
    assert cases.claim_lease(db, "CUT-noagent", "a")
    assert cases.claim_lease(db, "CUT-nolease", "a")
    assert not cases.claim_lease(db, "CUT-nolease", "b")
