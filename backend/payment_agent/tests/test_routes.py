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
