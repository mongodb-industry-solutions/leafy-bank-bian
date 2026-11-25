"""Case memory for the Reconciliation Agent: past exceptions retrieved by meaning.

Each investigated (and each backfilled historical) exception is described in a sentence or
two, embedded with Voyage, and stored on the exception itself as `caseMemory{}`. A new
investigation asks for "cases like this one" through Atlas `$vectorSearch` on `exceptions`,
pre-filtered to closed cases of the same category, and Voyage-reranks them against the
agent's description of what it is seeing.

This is vector search over **operational history**, not reference data — the line the
agentic handoff (2026-09-29) draws against Wei You's demo.

`caseMemory{}` is a sibling of `agent{}`, not inside it: `agent != null` means "the agent ran"
to the worker and the dashboard, and a backfilled HIST- row must not look worked. Every API
read projects `caseMemory` out (transactions/ledger `READ_PROJECTION`); so do the agent's
own reads, so the vector never reaches the model's context.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import semantic

logger = logging.getLogger(__name__)

COLLECTION = "exceptions"
FIELD = "caseMemory"
INDEX_NAME = "exceptions_case_memory_vector"
CLOSED = ("RESOLVED", "DISMISSED")

NUM_CANDIDATES = 100
VECTOR_LIMIT = 20
TOP_K = 5


def index_definition() -> dict:
    return {"fields": [
        {"type": "vector", "path": f"{FIELD}.embedding",
         "numDimensions": semantic.EMBED_DIMENSIONS, "similarity": "cosine"},
        {"type": "filter", "path": "category"},
        {"type": "filter", "path": "status"},
    ]}


def case_text(exc: dict, *, charge_bearer: Optional[str] = None) -> str:
    """What happened in this exception, as prose a reranker can compare."""
    agent = exc.get("agent") or {}
    historical = exc.get("historical") or {}
    detail = exc.get("detail") or {}
    resolution = exc.get("resolution") or {}
    bearer = charge_bearer or historical.get("chargeBearer")
    parts = [
        f"{exc.get('category')}.",
        f"Cause: {agent['cause']}." if agent.get("cause") else None,
        (f"Correspondent statement outcome: {historical['statementOutcome']}."
         if historical.get("statementOutcome") else None),
        f"Charge bearer: {bearer}." if bearer else None,
        (f"Discrepancy {detail['discrepancyAmount']}."
         if detail.get("discrepancyAmount") is not None else None),
        agent.get("rootCause"),
        agent.get("investigation"),
        (f"Resolved with {resolution['action']}: {resolution.get('note') or ''}".strip()
         if resolution.get("action") else None),
    ]
    return " ".join(p for p in parts if p)


def index_case(db: Any, exc: dict, *, correspondent_bic: Optional[str] = None,
               charge_bearer: Optional[str] = None) -> bool:
    """Embed one exception into its own `caseMemory{}`. False when Voyage is unavailable."""
    text = case_text(exc, charge_bearer=charge_bearer)
    vectors = semantic.embed([text], query=False)
    if not vectors:
        return False
    db[COLLECTION].update_one(
        {"exceptionId": exc["exceptionId"]},
        {"$set": {FIELD: {"text": text, "embedding": vectors[0],
                          "correspondentBic": correspondent_bic,
                          "model": semantic.embed_model(),
                          "indexedAt": datetime.now(timezone.utc)}}})
    return True


def similar_cases_pipeline(query_vector: list[float], category: str, exclude_id: str) -> list:
    return [
        {"$vectorSearch": {"index": INDEX_NAME, "path": f"{FIELD}.embedding",
                           "queryVector": query_vector,
                           "numCandidates": NUM_CANDIDATES, "limit": VECTOR_LIMIT,
                           "filter": {"category": category, "status": {"$in": list(CLOSED)}}}},
        {"$match": {"exceptionId": {"$ne": exclude_id}}},
        {"$project": {"_id": 0, "exceptionId": 1, "status": 1,
                      "text": f"${FIELD}.text", "correspondentBic": f"${FIELD}.correspondentBic",
                      "action": "$resolution.action", "note": "$resolution.note",
                      "vectorScore": {"$meta": "vectorSearchScore"}}},
    ]


def similar_cases(db: Any, exc: dict, situation: str,
                  correspondent_bic: Optional[str] = None) -> Optional[list[dict]]:
    """Closed cases most like `situation`, best first. None when semantic search is off."""
    query = (situation or "").strip() or case_text(exc)
    vectors = semantic.embed([query], query=True)
    if not vectors:
        return None
    try:
        rows = list(db[COLLECTION].aggregate(
            similar_cases_pipeline(vectors[0], exc.get("category"), exc.get("exceptionId"))))
    except Exception:  # noqa: BLE001 — no index yet, or not on Atlas
        logger.warning("case-memory $vectorSearch failed", exc_info=True)
        return None
    scores = semantic.rerank(query, [r.get("text") or "" for r in rows])
    for i, r in enumerate(rows):
        r["relevance"] = scores[i] if scores else None
        r["sameCorrespondent"] = bool(correspondent_bic) and r.get("correspondentBic") == correspondent_bic
    if scores:
        rows.sort(key=lambda r: -r["relevance"])
    return rows[:TOP_K]
