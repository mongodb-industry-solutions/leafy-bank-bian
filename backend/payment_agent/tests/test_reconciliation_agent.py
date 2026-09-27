"""Unit tests for the Reconciliation Agent's non-LLM logic.

The live Bedrock call + change stream are integration-tested separately. These pin the
contract: `record_investigation` writes only the reserved `agent{}` subdoc (never status /
category / resolution), confidence is gated, `investigate()` never raises, and the discrepancy
hint summarises the trace correctly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import reconciliation_agent as ra


class _FakeColl:
    def __init__(self, docs=None):
        self._docs = docs or []
        self.updates = []  # recorded update_one calls

    def _matches(self, q):
        for d in self._docs:
            if all(d.get(k) == v for k, v in q.items()):
                return d
        return None

    def find_one(self, q, proj=None):
        return self._matches(q)

    def find(self, q, proj=None):
        return [d for d in self._docs if all(d.get(k) == v for k, v in q.items())]

    def update_one(self, q, upd):
        self.updates.append((q, upd))
        return SimpleNamespace(matched_count=1 if self._matches(q) else 0)


class _FakeDB(dict):
    def __getitem__(self, name):
        return self.get(name) or _FakeColl()


def _msg(content):
    return SimpleNamespace(content=content)


# --- record_investigation tool ------------------------------------------------


def test_record_investigation_writes_agent_subdoc_only():
    db = _FakeDB()
    db["exceptions"] = _FakeColl([{"exceptionId": "EXC-1", "status": "OPEN",
                                   "category": "RECONCILIATION_DISCREPANCY"}])
    tools = ra._build_tools(db)
    record = next(t for t in tools if t.name == "record_investigation")
    fn = getattr(record, "func", record)
    out = fn(exception_id="EXC-1", root_cause="correspondent fee deducted",
            evidence=["settlementPositions.actualAmount=24975 vs expected 25000"],
            confidence="HIGH", recommended_resolution="ACCEPT_DISCREPANCY",
            investigation="$25 gap matches the wire fee.")
    assert "Recorded" in out
    assert len(db["exceptions"].updates) == 1
    q, upd = db["exceptions"].updates[0]
    assert q == {"exceptionId": "EXC-1"}
    set_op = upd["$set"]
    # The ONLY field touched is `agent` — never status/category/resolution.
    assert list(set_op.keys()) == ["agent"]
    assert set_op["agent"]["rootCause"] == "correspondent fee deducted"
    assert set_op["agent"]["confidence"] == "HIGH"
    assert set_op["agent"]["recommendedResolution"] == "ACCEPT_DISCREPANCY"
    assert "recordedAt" in set_op["agent"]


def test_record_investigation_rejects_bad_confidence():
    db = _FakeDB()
    db["exceptions"] = _FakeColl([{"exceptionId": "EXC-2"}])
    tools = ra._build_tools(db)
    record = next(t for t in tools if t.name == "record_investigation")
    fn = getattr(record, "func", record)
    out = fn(exception_id="EXC-2", root_cause="x", evidence=[], confidence="MAYBE",
            recommended_resolution="ACCEPT_DISCREPANCY")
    assert "confidence must be" in out
    assert db["exceptions"].updates == []  # nothing written


def test_record_investigation_missing_exception():
    db = _FakeDB()
    tools = ra._build_tools(db)
    record = next(t for t in tools if t.name == "record_investigation")
    fn = getattr(record, "func", record)
    out = fn(exception_id="EXC-NOPE", root_cause="x", evidence=[], confidence="LOW",
            recommended_resolution="manual review")
    assert "No exception found" in out


# --- discrepancy hint ---------------------------------------------------------


def test_discrepancy_hint_from_legs():
    legs = [{"discrepancyAmount": 25}]
    assert "25" in ra._discrepancy_hint(legs, [])

def test_discrepancy_hint_from_positions():
    positions = [{"expected": 25000, "actual": 24975}]
    hint = ra._discrepancy_hint([], positions)
    assert "25000" in hint and "24975" in hint


def test_discrepancy_hint_none():
    assert "No numeric discrepancy" in ra._discrepancy_hint([], [])


# --- investigate() degradation ------------------------------------------------


class _BoomAgent:
    def invoke(self, *a, **k):
        raise RuntimeError("bedrock down")


def test_investigate_never_raises():
    db = _FakeDB()
    # Agent raises — investigate returns None, the exception is unchanged.
    assert ra.investigate(_BoomAgent(), db, "EXC-9", "PAY-9") is None


# --- graph compile ------------------------------------------------------------


def test_build_reconciliation_agent_compiles():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    class _ToolBindFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = _ToolBindFake(responses=[AIMessage(content="investigation complete")])
    agent = ra.build_reconciliation_agent(model, _FakeDB(), InMemorySaver())
    assert hasattr(agent, "invoke")


def test_reconciliation_agent_hitl_gate_pauses_then_resumes():
    """The HITL approval gate: the compiled graph pauses at the `approval` node via
    `interrupt()`, and resuming with `Command(resume=True)` completes the graph. This is
    the LangGraph HITL primitive the Phase-2 agent showcases — agent investigates, surfaces its
    recommendation for operator review, pauses, resumes on acknowledgement. The operator
    still resolves via the existing transactions UI; this gate reviews, it does not execute.
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    class _ToolBindFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    # A final AIMessage with no tool calls → after_investigate routes to approval → interrupt.
    model = _ToolBindFake(responses=[AIMessage(content="investigation complete")])
    db = _FakeDB()
    db["exceptions"] = _FakeColl([{"exceptionId": "EXC-H", "status": "OPEN",
                                   "category": "RECONCILIATION_DISCREPANCY"}])
    agent = ra.build_reconciliation_agent(model, db, InMemorySaver())
    config = {"configurable": {"thread_id": "EXC-H"}}
    state = {
        "messages": [{"role": "user", "content": "investigate EXC-H"}],
        "exception_id": "EXC-H",
        "payment_id": "PAY-H",
    }

    # First invoke pauses at the approval gate (interrupt sentinel raised).
    result = agent.invoke(state, config=config)
    assert "__interrupt__" in result

    # Resume with the operator's acknowledgement — graph completes.
    resumed = agent.invoke(Command(resume=True), config=config)
    assert "__interrupt__" not in resumed
