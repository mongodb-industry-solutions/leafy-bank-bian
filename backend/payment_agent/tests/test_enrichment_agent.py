"""Unit tests for the Enrichment Agent's non-LLM logic.

The LangGraph graph + Bedrock call are integration-tested separately (need creds + the live
vector index). These tests pin the contract that keeps the saga safe: the proposal parser
is defensive, the field allowlist is enforced, and `propose()` never raises.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import enrichment_agent as ea
from reference_data import PurposeCodeMatch


def _msg(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content)


# --- parse_proposals ---------------------------------------------------------


def test_parse_clean_json():
    out = {"messages": [_msg('{"proposals": [{"field": "remittance.purposeCode", "to": "SUPP", "reason": "supplier payment"}]}')]}
    p = ea.parse_proposals(out)
    assert len(p) == 1
    assert p[0]["field"] == "remittance.purposeCode"
    assert p[0]["to"] == "SUPP"
    assert p[0]["source"] == "agent"


def test_parse_markdown_fenced_json():
    out = {"messages": [_msg('```json\n{"proposals": [{"field": "categoryPurpose", "to": "SUPP"}]}\n```')]}
    p = ea.parse_proposals(out)
    assert len(p) == 1
    assert p[0]["field"] == "categoryPurpose"


def test_parse_json_wrapped_in_prose():
    out = {"messages": [_msg('Here is my proposal: {"proposals": [{"field": "remittance.purposeCode", "to": "SALA"}]} thanks')]}
    assert len(ea.parse_proposals(out)) == 1


def test_parse_drops_disallowed_field():
    # The agent tried to change the debtor BIC — must be dropped, not applied.
    out = {"messages": [_msg('{"proposals": [{"field": "debtor.bic", "to": "BARCGB22"}, {"field": "remittance.purposeCode", "to": "SUPP"}]}')]}
    p = ea.parse_proposals(out)
    assert [x["field"] for x in p] == ["remittance.purposeCode"]


def test_parse_empty_proposals():
    out = {"messages": [_msg('{"proposals": []}')]}
    assert ea.parse_proposals(out) == []


def test_parse_malformed_returns_empty():
    assert ea.parse_proposals({"messages": [_msg("no json here at all")]}) == []
    assert ea.parse_proposals({"messages": []}) == []


def test_parse_drops_proposal_without_to():
    out = {"messages": [_msg('{"proposals": [{"field": "remittance.purposeCode"}]}')]}
    assert ea.parse_proposals(out) == []


# --- confidence + new allowlist fields ---------------------------------------

def test_parse_carries_confidence_when_valid():
    out = {"messages": [_msg(
        '{"proposals": [{"field": "remittance.invoiceNo", "to": "INV-48392", '
        '"reason": "extracted", "confidence": "high"}]}'
    )]}
    p = ea.parse_proposals(out)
    assert len(p) == 1
    assert p[0]["field"] == "remittance.invoiceNo"
    assert p[0]["to"] == "INV-48392"
    assert p[0]["confidence"] == "HIGH"  # normalised to upper


def test_parse_blanks_invalid_confidence():
    out = {"messages": [_msg(
        '{"proposals": [{"field": "remittance.reference", "to": "R-1", "confidence": "very"}]}'
    )]}
    p = ea.parse_proposals(out)
    assert p[0]["confidence"] == ""


def test_parse_accepts_remittance_reference_fields():
    out = {"messages": [_msg(
        '{"proposals": [{"field": "remittance.reference", "to": "FOLIO-99"}, '
        '{"field": "remittance.invoiceNo", "to": "INV-1"}]}'
    )]}
    fields = [x["field"] for x in ea.parse_proposals(out)]
    assert fields == ["remittance.reference", "remittance.invoiceNo"]


def test_parse_accepts_beneficiary_bank_fields():
    # Doina's flagship job — the agent resolves the beneficiary bank from the supplied BIC.
    out = {"messages": [_msg(
        '{"proposals": [{"field": "creditor.bankName", "to": "Barclays Bank PLC"}, '
        '{"field": "creditor.bankCountry", "to": "GB"}, '
        '{"field": "creditor.clearingSystemCode", "to": "USABA"}]}'
    )]}
    fields = [x["field"] for x in ea.parse_proposals(out)]
    assert fields == ["creditor.bankName", "creditor.bankCountry", "creditor.clearingSystemCode"]


# --- candidate trace (authoritative, from tool results) ----------------------

def _tool_msg(name: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, content=content)


def test_extract_considered_pulls_real_tool_results():
    tool_json = json.dumps([
        {"code": "SUPP", "name": "Supplier Payment", "description": "d",
         "category": "TRADE", "score": 0.91},
        {"code": "GDDS", "name": "Goods", "description": "d",
         "category": "TRADE", "score": 0.74},
    ])
    out = {"messages": [_tool_msg("purpose_code_resolve", tool_json), _msg('{"proposals": []}')]}
    considered = ea._extract_considered(out)
    assert [c["code"] for c in considered] == ["SUPP", "GDDS"]
    assert considered[0]["score"] == 0.91  # sorted desc


def test_extract_considered_dedupes_across_calls():
    one = json.dumps([{"code": "SUPP", "name": "Supplier", "description": "d",
                       "category": "TRADE", "score": 0.9}])
    two = json.dumps([{"code": "SUPP", "name": "Supplier", "description": "d",
                       "category": "TRADE", "score": 0.8},
                      {"code": "GDDS", "name": "Goods", "description": "d",
                       "category": "TRADE", "score": 0.6}])
    out = {"messages": [_tool_msg("purpose_code_resolve", one),
                        _tool_msg("purpose_code_resolve", two)]}
    considered = ea._extract_considered(out)
    assert [c["code"] for c in considered] == ["SUPP", "GDDS"]


def test_propose_attaches_considered_to_purpose_code_only():
    tool_json = json.dumps([{"code": "SUPP", "name": "Supplier", "description": "d",
                             "category": "TRADE", "score": 0.9}])

    class _Agent:
        def invoke(self, *a, **k):
            return {"messages": [
                _tool_msg("purpose_code_resolve", tool_json),
                _msg('{"proposals": [{"field": "remittance.purposeCode", "to": "SUPP", '
                     '"confidence": "HIGH"}, {"field": "remittance.invoiceNo", "to": "INV-1"}]}'),
            ]}

    p = ea.propose(_Agent(), {"rail": "WIRE", "remittance": {"unstructured": "invoice INV-1"}}, "PAY-T")
    by_field = {x["field"]: x for x in p}
    assert "considered" in by_field["remittance.purposeCode"]
    assert by_field["remittance.purposeCode"]["considered"][0]["code"] == "SUPP"
    # Extraction proposals get no candidate trace.
    assert "considered" not in by_field["remittance.invoiceNo"]


# --- tools (with a fake reference store) -------------------------------------


class _FakeRef:
    def purpose_codes_semantic(self, query_text, k=3):
        if query_text == "hit":
            return [PurposeCodeMatch(code="SUPP", name="Supplier Payment",
                                     description="desc", category="TRADE", score=0.9)]
        return []

    def bank_by_bic(self, bic):
        return None


def test_purpose_code_resolve_tool_returns_json(monkeypatch):
    tools = ea._build_tools(_FakeRef())
    resolve = next(t for t in tools if t.name == "purpose_code_resolve")
    # LangGraph @tool callables expose the wrapped fn via .func (or are directly callable
    # depending on version); invoke the underlying function to avoid tool-call protocol.
    fn = getattr(resolve, "func", resolve)
    result = fn(query_text="hit", k=3)
    parsed = json.loads(result)
    assert parsed[0]["code"] == "SUPP"
    assert parsed[0]["score"] == 0.9


def test_purpose_code_resolve_tool_empty(monkeypatch):
    tools = ea._build_tools(_FakeRef())
    resolve = next(t for t in tools if t.name == "purpose_code_resolve")
    fn = getattr(resolve, "func", resolve)
    assert "No purpose-code matches" in fn(query_text="miss", k=3)


# --- propose() degradation ---------------------------------------------------


class _BoomAgent:
    def invoke(self, *a, **k):
        raise RuntimeError("bedrock down")


class _FixedAgent:
    def __init__(self, content):
        self._content = content

    def invoke(self, *a, **k):
        return {"messages": [_msg(self._content)]}


def test_propose_never_raises_on_agent_failure():
    assert ea.propose(_BoomAgent(), {"rail": "WIRE"}, "PAY-1") == []


def test_propose_returns_parsed_proposals():
    agent = _FixedAgent('{"proposals": [{"field": "remittance.purposeCode", "to": "SUPP", "reason": "r"}]}')
    p = ea.propose(agent, {"rail": "WIRE", "amount": 25000, "currency": "USD"}, "PAY-2")
    assert len(p) == 1
    assert p[0]["to"] == "SUPP"


def test_propose_through_compiled_graph():
    """End-to-end through the real create_agent graph with a fake model (no Bedrock).

    The model returns a final AIMessage with no tool calls, so the agent loop terminates
    immediately and `propose()` parses the proposal. Pins the create_agent wiring
    (system_prompt, checkpointer, tools) without needing credentials or the vector index.
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    class _ToolBindFake(FakeMessagesListChatModel):
        # `create_agent` calls model.bind_tools(tools); the base fake raises NotImplementedError.
        # Returning self is enough for a no-tool-call terminal response.
        def bind_tools(self, tools, **kwargs):
            return self

    model = _ToolBindFake(
        responses=[AIMessage(content='{"proposals": [{"field": "remittance.purposeCode", "to": "SUPP", "reason": "supplier"}]}')]
    )
    agent = ea.build_enrichment_agent(model, _FakeRef(), InMemorySaver())
    p = ea.propose(agent, {"rail": "WIRE", "amount": 25000, "currency": "USD",
                           "remittance": {"unstructured": "payment for supplier invoice"}}, "PAY-G")
    assert len(p) == 1
    assert p[0]["field"] == "remittance.purposeCode"
    assert p[0]["to"] == "SUPP"
    assert p[0]["source"] == "agent"
