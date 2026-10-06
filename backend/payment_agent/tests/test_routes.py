"""The approve route: a decision is required, and nothing resumes unless a proposal is paused."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(monkeypatch):
    resumed = []
    monkeypatch.setattr(main, "RECON_AGENT", object())
    monkeypatch.setattr(main, "_DB", object())
    monkeypatch.setattr(main, "resume", lambda *a: resumed.append(a) or {"verification": {"result": "RESOLVED"}})
    # No lifespan: TestClient without `with` does not run it, so no Mongo/Bedrock is touched.
    c = TestClient(main.app)
    c.resumed = resumed
    return c


def test_a_bodyless_call_is_not_an_approval(client, monkeypatch):
    # The pre-Part-C "Acknowledge" button posts no body; it must never execute a proposal.
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    assert client.post("/reconciliation/EXC-1/approve").status_code == 422
    assert not client.resumed


def test_nothing_paused_is_a_conflict(client, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: False)
    r = client.post("/reconciliation/EXC-1/approve", json={"decision": "APPROVE"})
    assert r.status_code == 409 and not client.resumed


def test_approve_resumes_with_the_decision(client, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    r = client.post("/reconciliation/EXC-1/approve", json={"decision": "REJECT", "note": "n"})
    assert r.status_code == 200
    assert client.resumed[0][2:5] == ("EXC-1", "REJECT", "n")
    assert r.json()["agent"]["verification"]["result"] == "RESOLVED"


# --- Plan E: the read-only steps timeline --------------------------------------------------

def test_steps_without_a_thread_is_404(client, monkeypatch):
    monkeypatch.setattr(main, "thread_messages", lambda *a: [])
    assert client.get("/reconciliation/EXC-1/steps").status_code == 404


def test_steps_returns_the_shaped_timeline(client, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    monkeypatch.setattr(main, "thread_messages", lambda *a: [HumanMessage("go"), AIMessage("hmm")])
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    body = client.get("/reconciliation/EXC-1/steps").json()
    assert body["exceptionId"] == "EXC-1" and body["awaitingApproval"] is True
    assert [s["kind"] for s in body["steps"]] == ["note", "thought"]


def test_steps_before_the_agent_is_built_is_503(monkeypatch):
    monkeypatch.setattr(main, "RECON_AGENT", None)
    assert TestClient(main.app).get("/reconciliation/EXC-1/steps").status_code == 503


# --- wake: new evidence brings a scheduled recheck forward ---------------------------------

def test_wake_pulls_only_pending_rechecks_forward(client, monkeypatch):
    calls = []

    class _Coll:
        def update_many(self, query, update):
            calls.append((query, update))
            return type("R", (), {"modified_count": 1})()

    monkeypatch.setattr(main, "_DB", {"exceptions": _Coll()})
    r = client.post("/reconciliation/wake", json={"paymentId": "PAY-1"})
    assert r.json() == {"paymentId": "PAY-1", "woken": 1}
    query, update = calls[0]
    assert query["paymentId"] == "PAY-1" and query["status"] == "OPEN"
    assert query["agent.nextCheckAt"] == {"$ne": None}
    assert "agent.nextCheckAt" in update["$set"]


# --- Cutoff Agent routes (cutoff plan C3) --------------------------------------------------

from tests._fakes import FakeColl, FakeDB  # noqa: E402


@pytest.fixture
def cutoff(monkeypatch):
    db = FakeDB()
    db["cutoffCases"] = FakeColl([
        {"caseId": "CUT-1", "paymentId": "PAY-1", "clockRunId": "RUN-1", "status": "OPEN",
         "active": True, "updatedAt": 1},
        {"caseId": "CUT-2", "paymentId": "PAY-2", "clockRunId": "RUN-1", "status": "ACTIONED",
         "active": False, "updatedAt": 2},
    ])
    resumed = []
    monkeypatch.setattr(main, "CUTOFF_AGENT", object())
    monkeypatch.setattr(main, "_DB", db)
    monkeypatch.setattr(main.cutoff_agent, "resume", lambda *a: resumed.append(a))
    c = TestClient(main.app)
    c.resumed = resumed
    return c


def test_health_reports_the_cutoff_agent(cutoff):
    assert cutoff.get("/health").json()["cutoffAgentReady"] is True


def test_cutoff_cases_list_filters_newest_first(cutoff):
    assert [c["caseId"] for c in cutoff.get("/cutoff/cases").json()["cases"]] == ["CUT-2", "CUT-1"]
    body = cutoff.get("/cutoff/cases", params={"active": "true", "clockRunId": "RUN-1"}).json()
    assert [c["caseId"] for c in body["cases"]] == ["CUT-1"]


def test_cutoff_case_read_and_404(cutoff):
    assert cutoff.get("/cutoff/cases/CUT-1").json()["case"]["paymentId"] == "PAY-1"
    assert cutoff.get("/cutoff/cases/CUT-9").status_code == 404


def test_cutoff_routes_without_a_db_are_503(monkeypatch):
    monkeypatch.setattr(main, "_DB", None)
    monkeypatch.setattr(main, "CUTOFF_AGENT", None)
    c = TestClient(main.app)
    assert c.get("/cutoff/cases").status_code == 503
    assert c.get("/cutoff/cases/CUT-1/steps").status_code == 503
    assert c.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"}).status_code == 503
    assert c.post("/cutoff/sweep", json={}).status_code == 503


def test_cutoff_steps_without_a_thread_is_404(cutoff, monkeypatch):
    monkeypatch.setattr(main, "thread_messages", lambda *a: [])
    assert cutoff.get("/cutoff/cases/CUT-1/steps").status_code == 404


def test_cutoff_approve_requires_a_decision_and_a_paused_proposal(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    assert cutoff.post("/cutoff/cases/CUT-1/approve").status_code == 422
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: False)
    assert cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"}).status_code == 409
    assert not cutoff.resumed


def test_cutoff_approve_resumes_and_returns_the_case(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    r = cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE", "by": "kiran"})
    assert r.status_code == 200
    assert cutoff.resumed[0][2:] == ("CUT-1", "APPROVE", None, "kiran")
    assert r.json()["case"]["caseId"] == "CUT-1" and r.json()["decision"] == "APPROVE"


def test_cutoff_approve_resume_failure_is_502(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)

    def boom(*a):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(main.cutoff_agent, "resume", boom)
    assert cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "REJECT"}).status_code == 502


def test_cutoff_sweep_scopes_to_a_payment(cutoff, monkeypatch):
    monkeypatch.setenv("ENABLE_CUTOFF_AGENT", "true")
    calls = []
    monkeypatch.setattr(main.cutoff_worker, "sweep_once",
                        lambda agent, db, **kw: calls.append(kw) or {"evaluated": 1, "investigated": 1, "closed": 0})
    assert cutoff.post("/cutoff/sweep", json={"paymentId": "PAY-1"}).json()["investigated"] == 1
    assert cutoff.post("/cutoff/sweep").status_code == 200
    assert calls[0] == {"payment_id": "PAY-1", "source": "MANUAL"} and calls[1]["payment_id"] is None


def test_cutoff_sweep_is_503_when_the_flag_is_off(cutoff, monkeypatch):
    monkeypatch.delenv("ENABLE_CUTOFF_AGENT", raising=False)
    monkeypatch.setattr(main.cutoff_worker, "sweep_once", lambda *a, **k: pytest.fail("swept"))
    assert cutoff.post("/cutoff/sweep", json={}).status_code == 503


def test_cutoff_approve_while_the_case_is_claimed_is_409(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    assert main.cutoff_worker._claim("CUT-1")
    try:
        r = cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"})
    finally:
        main.cutoff_worker._release("CUT-1")
    assert r.status_code == 409 and not cutoff.resumed


def test_cutoff_approve_while_another_instance_leases_the_case_is_409(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    assert main.cutoff_cases.claim_lease(main._DB, "CUT-1", "other-instance")
    assert cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"}).status_code == 409
    assert not cutoff.resumed


def test_cutoff_approve_releases_the_claim_and_lease(cutoff, monkeypatch):
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: True)
    assert cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"}).status_code == 200
    assert main.cutoff_worker._claim("CUT-1")
    main.cutoff_worker._release("CUT-1")
    assert main.cutoff_cases.get(main._DB, "CUT-1")["agent"]["lease"] is None


def test_cutoff_approve_failure_after_the_pause_is_spent_says_so(cutoff, monkeypatch):
    paused = [True]
    monkeypatch.setattr(main, "is_awaiting_approval", lambda *a: paused[0])

    def boom(*a):
        paused[0] = False
        raise RuntimeError("transactions 503")

    monkeypatch.setattr(main.cutoff_agent, "resume", boom)
    r = cutoff.post("/cutoff/cases/CUT-1/approve", json={"decision": "APPROVE"})
    assert r.status_code == 502 and "no longer paused" in r.json()["detail"]
