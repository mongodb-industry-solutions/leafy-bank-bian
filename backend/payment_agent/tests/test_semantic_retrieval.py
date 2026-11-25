"""Semantic retrieval for the Reconciliation Agent: case memory (#1) and candidate rerank (#2).

Voyage is always stubbed (conftest blocks the real client). What is pinned: degradation to
the deterministic path, rerank only breaking ties, the `$vectorSearch` pipeline's filters,
and that an investigation lands in case memory without touching `exceptions`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import case_memory
import policy
import reconciliation_agent as ra
import recon_evidence as ev
import semantic
from tests._fakes import FakeColl, FakeDB


# --- semantic.py ------------------------------------------------------------------------

def test_without_a_key_semantic_calls_return_none():
    assert not semantic.enabled()
    assert semantic.embed(["x"], query=True) is None
    assert semantic.rerank("q", ["a"]) is None


def test_rerank_returns_scores_in_document_order(monkeypatch):
    results = [SimpleNamespace(index=1, relevance_score=0.9),
               SimpleNamespace(index=0, relevance_score=0.2)]
    client = SimpleNamespace(rerank=lambda *a, **k: SimpleNamespace(results=results))
    monkeypatch.setattr(semantic, "_client", lambda: client)
    assert semantic.rerank("q", ["a", "b"]) == [0.2, 0.9]


def test_a_voyage_error_degrades_to_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429")
    client = SimpleNamespace(embed=boom, rerank=boom)
    monkeypatch.setattr(semantic, "_client", lambda: client)
    assert semantic.embed(["x"], query=False) is None
    assert semantic.rerank("q", ["a"]) is None


# --- #2 candidate rerank ----------------------------------------------------------------

def _row(ref, narrative, amount=100.0):
    return {"reference": ref, "paymentId": "PAY-aa11", "amount": amount,
            "expectedAmount": 100.0, "narrative": narrative}


def test_rerank_breaks_ties_on_the_reference_score():
    rows = [_row("ORPH-1", "FX SWAP LEG"), _row("ORPH-2", "INVOICE 4471 ACME CORP")]
    ranked = ev.rank_candidates(rows, semantic=[0.1, 0.8])
    assert [c["reference"] for c in ranked] == ["ORPH-2", "ORPH-1"]
    assert ranked[0]["semanticScore"] == 0.8


def test_rerank_never_lifts_a_weaker_reference_match():
    rows = [_row("AA11/LEAFYBK", "NOTHING ALIKE"), _row("ORPH-2", "INVOICE 4471 ACME CORP")]
    ranked = ev.rank_candidates(rows, semantic=[0.0, 0.99])
    assert ranked[0]["reference"] == "AA11/LEAFYBK"


def test_without_rerank_the_order_is_unchanged_and_scores_are_null():
    rows = [_row("ORPH-1", "A", amount=99.0), _row("ORPH-2", "B", amount=100.0)]
    ranked = ev.rank_candidates(rows)
    assert ranked[0]["reference"] == "ORPH-2"  # amount closeness, as before
    assert all(c["semanticScore"] is None for c in ranked)


def test_find_candidates_reranks_lines_against_the_payment_narrative(monkeypatch):
    rows = [_row("ORPH-1", "FX SWAP LEG"), _row("ORPH-2", "INVOICE 4471 ACME CORP")]
    monkeypatch.setattr(ev, "_lines_for_payment", lambda db, pid: (rows, "Invoice 4471 Acme Corp"))
    seen = {}

    def rerank(query, docs):
        seen.update(query=query, docs=docs)
        return [0.1, 0.9]
    exc = {"category": policy.CATEGORY_MISSING, "paymentId": "PAY-aa11"}
    ranked = ev.find_candidates(FakeDB(), exc, rerank=rerank)
    assert seen == {"query": "Invoice 4471 Acme Corp",
                    "docs": ["FX SWAP LEG", "INVOICE 4471 ACME CORP"]}
    assert ranked[0]["reference"] == "ORPH-2"


def test_no_narrative_means_no_rerank_call(monkeypatch):
    monkeypatch.setattr(ev, "_lines_for_payment",
                        lambda db, pid: ([_row("ORPH-1", None)], "Invoice 1"))
    exc = {"category": policy.CATEGORY_MISSING, "paymentId": "PAY-aa11"}
    ranked = ev.find_candidates(FakeDB(), exc, rerank=lambda *a: pytest.fail("called"))
    assert ranked[0]["semanticScore"] is None


def test_payment_narrative_joins_remittance_and_beneficiary():
    assert ev.payment_narrative({"remittance": {"unstructured": "Invoice 4471"},
                                 "creditor": {"name": "Acme Corp"}}) == "Invoice 4471 Acme Corp"
    assert ev.payment_narrative({}) is None


# --- #1 case memory ---------------------------------------------------------------------

_HIST = {"exceptionId": "EXC-H1", "category": policy.CATEGORY_DISCREPANCY, "status": "RESOLVED",
         "detail": {"discrepancyAmount": 25.0},
         "resolution": {"action": "POST_ADJUSTMENT", "note": "Correspondent deducted its charge."},
         "historical": {"correspondentBic": "DEUTDEFF", "chargeBearer": "DEBT",
                        "statementOutcome": "FEE_DEDUCTED"}}


def test_case_text_describes_a_historical_precedent():
    text = case_memory.case_text(_HIST)
    for part in ("RECONCILIATION_DISCREPANCY", "FEE_DEDUCTED", "DEBT", "25.0",
                 "POST_ADJUSTMENT", "deducted its charge"):
        assert part in text


def test_index_case_writes_case_memory_onto_the_exception(monkeypatch):
    monkeypatch.setattr(semantic, "embed", lambda texts, query: [[0.1, 0.2]])
    db = FakeDB()
    db["exceptions"] = FakeColl([{**_HIST, "agent": None}])
    assert case_memory.index_case(db, _HIST, correspondent_bic="DEUTDEFF")
    assert case_memory.index_case(db, _HIST, correspondent_bic="DEUTDEFF")  # idempotent
    docs = db["exceptions"].docs
    assert len(docs) == 1
    memory = docs[0]["caseMemory"]
    assert memory["embedding"] == [0.1, 0.2] and memory["correspondentBic"] == "DEUTDEFF"
    assert docs[0]["agent"] is None  # a backfilled precedent must not look worked


def test_index_case_is_a_no_op_without_voyage():
    db = FakeDB()
    db["exceptions"] = FakeColl([dict(_HIST)])
    assert not case_memory.index_case(db, _HIST)
    assert "caseMemory" not in db["exceptions"].docs[0]


def test_the_pipeline_prefilters_category_and_closed_status_and_drops_self():
    stages = case_memory.similar_cases_pipeline([0.1], "CAT", "EXC-SELF")
    vs = stages[0]["$vectorSearch"]
    assert vs["index"] == case_memory.INDEX_NAME and vs["path"] == "caseMemory.embedding"
    assert vs["filter"] == {"category": "CAT", "status": {"$in": ["RESOLVED", "DISMISSED"]}}
    assert vs["numCandidates"] >= vs["limit"]
    assert {"$match": {"exceptionId": {"$ne": "EXC-SELF"}}} in stages
    assert "embedding" not in json.dumps(stages[-1])  # the vector never comes back


def test_the_index_definition_covers_the_vector_and_both_filters():
    fields = case_memory.index_definition()["fields"]
    assert {"type": "filter", "path": "category"} in fields
    assert {"type": "filter", "path": "status"} in fields
    vector = next(f for f in fields if f["type"] == "vector")
    assert vector["path"] == "caseMemory.embedding" and vector["numDimensions"] == 1024


class _AggColl(FakeColl):
    def __init__(self, rows=None, error=None):
        super().__init__()
        self.rows, self.error, self.pipeline = rows or [], error, None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        if self.error:
            raise self.error
        return [dict(r) for r in self.rows]


def test_similar_cases_are_reranked_and_flag_the_same_correspondent(monkeypatch):
    monkeypatch.setattr(semantic, "embed", lambda texts, query: [[0.3]])
    monkeypatch.setattr(semantic, "rerank", lambda q, docs: [0.2, 0.95])
    db = FakeDB()
    db[case_memory.COLLECTION] = _AggColl([
        {"exceptionId": "EXC-A", "text": "late line", "correspondentBic": "CHASUS33"},
        {"exceptionId": "EXC-B", "text": "charge deducted", "correspondentBic": "DEUTDEFF"}])
    exc = {"exceptionId": "EXC-NEW", "category": policy.CATEGORY_DISCREPANCY}
    out = case_memory.similar_cases(db, exc, "short by 25.00, DEBT wire", correspondent_bic="DEUTDEFF")
    assert [r["exceptionId"] for r in out] == ["EXC-B", "EXC-A"]
    assert out[0]["sameCorrespondent"] and not out[1]["sameCorrespondent"]
    assert out[0]["relevance"] == 0.95


@pytest.mark.parametrize("setup", ["no_voyage", "no_index"])
def test_similar_cases_is_none_when_semantic_search_is_unavailable(monkeypatch, setup):
    db = FakeDB()
    if setup == "no_index":
        monkeypatch.setattr(semantic, "embed", lambda texts, query: [[0.3]])
        db[case_memory.COLLECTION] = _AggColl(error=RuntimeError("index not found"))
    exc = {"exceptionId": "EXC-NEW", "category": policy.CATEGORY_DISCREPANCY}
    assert case_memory.similar_cases(db, exc, "x") is None


# --- agent wiring -----------------------------------------------------------------------

def _agent_db():
    db = FakeDB()
    db["exceptions"] = FakeColl([{
        "exceptionId": "EXC-R1", "paymentId": "PAY-R1", "status": "OPEN", "agent": None,
        "category": policy.CATEGORY_DISCREPANCY, "detail": {"discrepancyAmount": 25.0}}])
    db["payments"] = FakeColl([{"paymentId": "PAY-R1", "chargeBearer": "DEBT",
                                "correspondent": {"correspondentBic": "DEUTDEFF"}}])
    return db


def _tool(db, name):
    return next(t for t in ra._build_tools(db) if t.name == name)


_STATE = {"exception_id": "EXC-R1", "payment_id": "PAY-R1", "messages": []}


def test_recording_an_investigation_adds_it_to_case_memory(monkeypatch):
    monkeypatch.setattr(semantic, "embed", lambda texts, query: [[0.5]])
    db = _agent_db()
    _tool(db, "record_investigation").invoke({
        "state": _STATE, "cause": "FEE", "root_cause": "correspondent charge",
        "evidence": ["25.00 levied"], "confidence": "HIGH"})
    memory = db["exceptions"].docs[0]["caseMemory"]
    assert memory["correspondentBic"] == "DEUTDEFF"
    assert "correspondent charge" in memory["text"] and "DEBT" in memory["text"]
    # The agent's own reads never carry the vector into the model's context.
    assert "caseMemory" not in ra._exception(db, "EXC-R1")


def test_a_case_memory_failure_never_fails_the_investigation(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(case_memory, "index_case", boom)
    db = _agent_db()
    out = _tool(db, "record_investigation").invoke({
        "state": _STATE, "cause": "FEE", "root_cause": "x", "evidence": [], "confidence": "HIGH"})
    assert out.startswith("Recorded")


def test_precedents_return_both_lists_and_pass_the_situation(monkeypatch):
    seen = {}
    monkeypatch.setattr(ev, "resolution_precedents", lambda db, bic, cat: [{"action": "ACCEPT"}])

    def similar(db, exc, situation, correspondent_bic=None):
        seen.update(situation=situation, bic=correspondent_bic)
        return [{"exceptionId": "EXC-B"}]
    monkeypatch.setattr(case_memory, "similar_cases", similar)
    out = json.loads(_tool(_agent_db(), "get_resolution_precedents").invoke(
        {"state": _STATE, "situation": "short by 25.00"}))
    assert out == {"sameCorrespondent": [{"action": "ACCEPT"}],
                   "similarCases": [{"exceptionId": "EXC-B"}]}
    assert seen == {"situation": "short by 25.00", "bic": "DEUTDEFF"}


def test_precedents_degrade_to_the_correspondent_list(monkeypatch):
    monkeypatch.setattr(ev, "resolution_precedents", lambda db, bic, cat: [])
    out = json.loads(_tool(_agent_db(), "get_resolution_precedents").invoke({"state": _STATE}))
    assert out == {"sameCorrespondent": [], "similarCases": None}
