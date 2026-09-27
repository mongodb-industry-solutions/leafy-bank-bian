"""The Phase-1 Enrichment Agent — a LangGraph `create_agent` over Atlas Vector Search.

Invoked synchronously by the transactions service at the Stage-3 gate (between VALIDATED
and the deterministic planner's writes). Per the locked design (option B), the agent **only
proposes** enrichments — it never writes the payment document. The transactions service's
`enrichment.run` applies the proposals it accepts through its existing atomic `$set`, tagging
them `source: "agent"`.

## Scope (deliberately narrow)

The deterministic planner (`enrichment_plan.plan()`) validates a supplied purpose code
against the table and stamps our own (debtor/internal-creditor) bank identity. Three things
are this agent's jobs:

1. **Beneficiary-bank association** — when the customer supplied a BIC, retrieve and associate
   the normalized beneficiary-bank identity, country, clearing membership, and
   routing/addressability from the institution directory (Sep 17 draft L665-689). The agent's
   `reference_data_lookup` tool is load-bearing here. The transactions service re-validates
   the proposed BIC against its own directory port and applies the directory's canonical
   values — the agent proposes, the deterministic table gates.
2. **Semantic purpose-code resolution** — turning the customer's free-text remittance purpose
   into an ISO 20022 ExternalPurpose code (doc 17 §6, Q27).
3. **Remittance-reference extraction** — pulling a structured `remittance.invoiceNo` /
   `remittance.reference` out of the customer's free-text `remittance.unstructured` when they
   did not supply one directly. NLP extraction over the snapshot text; no reference table.

Mandatory customer-provided fields (debtor, amount, currency, and the BIC itself) are
off-limits — the spec is explicit: "the agent does not invent or replace mandatory
customer-provided payment information" (L679). The agent resolves *from* the BIC; it does not
fabricate one.

## Proposal shape

The agent's final message is a JSON object:

    {"proposals": [{"field": "remittance.purposeCode", "to": "SUPP",
                    "reason": "...", "confidence": "HIGH"}]}

Parsed defensively (strip code fences, `json.loads`, allowlist the `field`, validate
`confidence`). The candidate purpose-code trace (`considered[]` with real match scores) is
captured server-side from the `purpose_code_resolve` tool results in `propose()`, NOT trusted
from the model's self-report — an LLM's restated scores are not the actual vector-search
scores. Any parse failure, model error, or missing credentials degrade to `[]` — the saga
proceeds deterministically. This is the same best-effort posture as the reference-data port.

## Checkpointer

`MongoDBSaver` persists agent state in Atlas (dogfooding Mongo), keyed by `thread_id =
paymentId`. Required for any future HITL on this agent; harmless now since the agent runs to
completion in one invoke.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import tool

from reference_data import PurposeCodeMatch, ReferenceData

logger = logging.getLogger(__name__)

# The only fields the agent may propose to change. Everything else on the payment document
# is either deterministic-planner territory or mandatory customer input the agent must not
# touch (spec L679). Three proposal kinds:
# - purpose-code pair: semantic classification against the `purposeCodes` table.
# - remittance-reference pair: NLP extraction from free-text `remittance.unstructured`.
# - beneficiary-bank fields: resolved from the customer-supplied BIC via the institution
#   directory (`reference_data_lookup`). This is Doina's flagship enrichment-agent job
#   (Sep 17 draft L665-689): the agent retrieves and associates the normalized
#   beneficiary-bank identity, country, clearing membership, and routing/addressability.
ALLOWED_PROPOSAL_FIELDS = frozenset({
    "remittance.purposeCode",
    "categoryPurpose",
    "remittance.reference",
    "remittance.invoiceNo",
    "creditor.bic",
    "creditor.bankName",
    "creditor.bankCountry",
    "creditor.clearingSystemMemberId",
    "creditor.clearingSystemCode",
})

ENRICHMENT_SYSTEM_PROMPT = """\
You are the Payment Enrichment Agent for Leafy Bank, operating inside the BIAN Payment Order
Initiation service domain at Stage 3 (Payment Validation & Enrichment).

You receive a VALIDATED payment instruction. You do three jobs, all best-effort:

1. BENEFICIARY BANK (reference-data association). If the customer supplied a `creditor.bic`
   but the beneficiary-bank identity is not fully present (bank name, country, clearing-system
   code, clearing-system member id), call `reference_data_lookup` with the BIC and propose the
   resolved bank fields. This is your flagship job: retrieve and associate the normalized
   beneficiary-bank identity, country, clearing membership, and routing/addressability from
   the institution directory. Never invent a BIC the customer did not supply — you resolve
   *from* their BIC, you do not fabricate one. Do not overwrite a bank field the customer
   already supplied.

2. PURPOSE CODE (semantic classification). Propose an ISO 20022 ExternalPurpose code for the
   payment's purpose, derived from the customer's free-text remittance information, by
   semantically matching it against the `purposeCodes` reference table.

3. REMITTANCE REFERENCES (NLP extraction). If the customer's `remittance.unstructured` text
   contains an invoice number or a structured reference, and the customer did NOT already
   supply `remittance.invoiceNo` / `remittance.reference`, extract it and propose it.

Rules:
- Use `reference_data_lookup` with the creditor's BIC to resolve the beneficiary bank, then
  propose the bank fields it returns. You may NOT propose changes to the debtor, amount,
  currency, or the BIC itself (the BIC is the lookup key, not something you invent).
- Use `purpose_code_resolve` to semantically search the purpose-code table using the
  customer's remittance text (or the payment's stated purpose). Call it with the most
  descriptive phrase of what the payment is for.
- Only propose a purpose code if you are confident it fits. If the top match is weak or
  ambiguous, propose nothing for it.
- Only extract a reference that is actually present in the free text — do NOT invent one. If
  the text has no invoice/reference, propose nothing for those fields. Never propose a
  reference the customer already supplied.

You may propose any of: `creditor.bic`, `creditor.bankName`, `creditor.bankCountry`,
`creditor.clearingSystemMemberId`, `creditor.clearingSystemCode`, `remittance.purposeCode`,
`categoryPurpose`, `remittance.invoiceNo`, `remittance.reference`.

Each proposal must carry a `confidence` of HIGH, MEDIUM, or LOW reflecting how sure you are.
Respond with ONLY a JSON object, no prose, no markdown fences:
{"proposals": [
  {"field": "creditor.bankName", "to": "Barclays Bank PLC", "reason": "resolved from BIC BARCGB22", "confidence": "HIGH"},
  {"field": "remittance.purposeCode", "to": "SUPP", "reason": "supplier payment", "confidence": "HIGH"},
  {"field": "remittance.invoiceNo", "to": "INV-48392", "reason": "extracted from remittance", "confidence": "HIGH"}
]}

If you have no confident proposal, respond with: {"proposals": []}
"""


def _build_tools(reference_data: ReferenceData):
    """Build the agent's read-only tools, closing over the reference-data store."""

    @tool
    def purpose_code_resolve(query_text: str, k: int = 3) -> str:
        """Semantically search the ISO 20022 purpose-code table for the customer's remittance
        purpose. Returns the top-k matches as code / name / description / category / score.
        Use the most descriptive phrase of what the payment is for as `query_text`."""
        logger.info("purpose_code_resolve called: query_text=%r k=%d", query_text[:80], k)
        matches: list[PurposeCodeMatch] = reference_data.purpose_codes_semantic(
            query_text, k=k
        )
        if not matches:
            return "No purpose-code matches found."
        return json.dumps(
            [
                {
                    "code": m.code,
                    "name": m.name,
                    "description": m.description,
                    "category": m.category,
                    "score": round(m.score, 4),
                }
                for m in matches
            ]
        )

    @tool
    def reference_data_lookup(bic: str) -> str:
        """Resolve a BIC to the beneficiary bank's name, country, and clearing-member id."""
        logger.info("reference_data_lookup called: bic=%r", bic)
        bank = reference_data.bank_by_bic(bic)
        if bank is None:
            return f"No bank found for BIC {bic}."
        return json.dumps(
            {
                "bic": bank.bic,
                "bankName": bank.bank_name,
                "country": bank.bank_country,
                "clearingSystemCode": bank.clearing_system_code,
                "clearingSystemMemberId": bank.clearing_system_member_id,
                "city": bank.city,
            }
        )

    return [purpose_code_resolve, reference_data_lookup]


def build_enrichment_agent(model: Any, reference_data: ReferenceData, checkpointer: Any):
    """Assemble the `create_agent` graph with its tools and checkpointer."""
    from langchain.agents import create_agent

    tools = _build_tools(reference_data)
    return create_agent(
        model,
        tools=tools,
        system_prompt=ENRICHMENT_SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Free-text label, not a numeric score — an LLM's numeric confidence is not calibrated. The
# operator sees the label. Mirrors the Reconciliation Agent's discipline.
_CONFIDENCE_VALUES = frozenset({"HIGH", "MEDIUM", "LOW"})


def parse_proposals(agent_output: dict) -> list[dict]:
    """Extract the proposals list from the agent's final message, defensively.

    Accepts a JSON object possibly wrapped in markdown fences. Drops any proposal whose
    `field` is not in the allowlist — the agent is not permitted to touch other fields.
    Validates `confidence` against {HIGH, MEDIUM, LOW}; an absent/invalid value becomes "".
    The `considered` candidate trace is NOT read here — it is captured from real tool results
    in `propose()`. Returns [] on any parse failure.
    """
    messages = agent_output.get("messages") or []
    if not messages:
        return []
    content = getattr(messages[-1], "content", "") or ""
    if not isinstance(content, str) or not content.strip():
        return []
    text = _FENCE_RE.sub("", content.strip()).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap the JSON in a sentence; try to slice out the outermost braces.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("enrichment agent: could not parse proposals from %r", text[:200])
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("enrichment agent: could not parse proposals from %r", text[:200])
            return []
    raw = parsed.get("proposals") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []
    proposals = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if field not in ALLOWED_PROPOSAL_FIELDS:
            logger.info("enrichment agent: dropping disallowed proposal field %r", field)
            continue
        if not item.get("to"):
            continue
        confidence = str(item.get("confidence", "")).upper()
        if confidence not in _CONFIDENCE_VALUES:
            confidence = ""
        proposals.append(
            {
                "field": field,
                "to": item["to"],
                "reason": str(item.get("reason", "")),
                "confidence": confidence,
                "source": "agent",
            }
        )
    return proposals


def _extract_considered(agent_output: dict) -> list[dict]:
    """Pull the real `purpose_code_resolve` tool results out of the message history.

    The agent's final JSON may restate the candidates, but an LLM's restated scores are not
    the actual vector-search scores. The `ToolMessage`s in the graph output carry the exact
    JSON the tool returned, so they are the authoritative candidate trace. Returns the merged
    top matches across all calls (the agent may call the tool more than once), deduped on
    `code`, sorted by score descending.
    """
    messages = agent_output.get("messages") or []
    seen: dict[str, dict] = {}
    for msg in messages:
        # ToolMessage carries the tool name; fall back to a name attribute for non-LC types.
        name = getattr(msg, "name", None) or getattr(msg, "tool_name", None)
        if name != "purpose_code_resolve":
            continue
        content = getattr(msg, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        for m in parsed if isinstance(parsed, list) else []:
            code = m.get("code")
            if not code or code in seen:
                continue
            seen[code] = {
                "code": code,
                "name": m.get("name"),
                "description": m.get("description"),
                "category": m.get("category"),
                "score": m.get("score"),
            }
    return sorted(seen.values(), key=lambda c: c.get("score") or 0.0, reverse=True)


def propose(
    agent: Any,
    payment_snapshot: dict,
    payment_id: str,
) -> list[dict]:
    """Run the enrichment agent and return its proposals. Never raises.

    `payment_snapshot` is the validated payment document (or a trimmed view of it) — the
    agent reasons over it but does not write it back. `payment_id` keys the checkpointer
    thread. Any failure (model error, no credentials, timeout) returns [] so the
    transactions saga proceeds deterministically.

    The purpose-code proposal is enriched with `considered[]` — the real candidate matches
    captured from the `purpose_code_resolve` tool results, not the model's self-report.
    """
    user_msg = (
        f"Payment {payment_id} (rail={payment_snapshot.get('rail')}, "
        f"amount={payment_snapshot.get('amount')} {payment_snapshot.get('currency')}):\n"
        f"{json.dumps(payment_snapshot, default=str)}"
    )
    try:
        output = agent.invoke(
            {"messages": [{"role": "user", "content": user_msg}]},
            config={"configurable": {"thread_id": payment_id}},
        )
    except Exception:  # noqa: BLE001 — never block the saga.
        logger.warning(
            "enrichment agent invoke failed for %s — returning no proposals",
            payment_id,
            exc_info=True,
        )
        return []
    final = getattr((output.get("messages") or [{}])[-1], "content", "") or ""
    proposals = parse_proposals(output)
    # Attach the authoritative candidate trace to the purpose-code proposal(s). Extraction
    # proposals (invoiceNo/reference) have no candidate trace — they're NLP over the snapshot.
    if proposals:
        considered = _extract_considered(output)
        if considered:
            for p in proposals:
                if p["field"] in ("remittance.purposeCode", "categoryPurpose"):
                    p["considered"] = considered
    logger.info(
        "enrichment agent %s: final message=%r proposals=%r",
        payment_id, final[:200], proposals,
    )
    return proposals
