"""Payment Agent service — Phase-1 Enrichment + Reconciliation agents.

Two LangGraph agents over Atlas Vector Search + Bedrock (Haiku), both in this one service so the
money-path transactions service stays thin:

- **Enrichment Agent** — synchronous, called by the transactions service at the Stage-3 gate.
  `POST /enrichment/propose` → proposes purpose-code enrichments (option B: proposes only).
- **Reconciliation Agent** — asynchronous, change-stream-driven. Watches `exceptions` for OPEN
  `RECONCILIATION_DISCREPANCY` inserts, investigates the mismatch, writes findings onto
  `exceptions.agent{}`. `POST /reconciliation/investigate` triggers a run manually;
  `GET /reconciliation/{exception_id}` reads the recorded investigation.

Both share the Bedrock model + `MongoDBSaver` checkpointer (state persists in Atlas).

Run: ``uvicorn main:app --port 8004`` (or ``python -m main``).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from bedrock import bedrock_model
from database.connection import MongoDBConnection
from enrichment_agent import build_enrichment_agent, propose
from reconciliation_agent import build_reconciliation_agent, investigate
from reconciliation_worker import start_reconciliation_worker
from reference_data import ReferenceData, VoyageEmbedder

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ENRICHMENT_AGENT: Optional[Any] = None
RECON_AGENT: Optional[Any] = None
_DB: Optional[Any] = None


def _build(db: Any, checkpointer: Any) -> tuple[Any, Any]:
    model = bedrock_model()
    reference_data = ReferenceData(db, embedder=VoyageEmbedder())
    enrichment = build_enrichment_agent(model, reference_data, checkpointer)
    reconciliation = build_reconciliation_agent(model, db, checkpointer)
    return enrichment, reconciliation


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ENRICHMENT_AGENT, RECON_AGENT, _DB
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (see backend/payment_agent/.env).")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    try:
        connection = MongoDBConnection(uri)
        db = connection.get_database(db_name)
        from langgraph.checkpoint.mongodb import MongoDBSaver
        checkpointer = MongoDBSaver(connection.client)
        ENRICHMENT_AGENT, RECON_AGENT = _build(db, checkpointer)
        _DB = db
        logger.info("Payment agents built (enrichment + reconciliation).")
        start_reconciliation_worker(RECON_AGENT, db, connection)
    except Exception:  # noqa: BLE001
        logger.warning("Payment agents could not be built at startup — endpoints will 503/degrade.",
                       exc_info=True)
    yield
    ENRICHMENT_AGENT = RECON_AGENT = _DB = None


app = FastAPI(title="Leafy Bank Payment Agent", lifespan=lifespan)


class ProposeRequest(BaseModel):
    paymentId: str
    payment: dict = Field(..., description="Validated payment snapshot (read-only context).")


class Proposal(BaseModel):
    field: str
    to: str
    reason: str = ""
    source: str = "agent"
    # HIGH/MEDIUM/LOW free-text label (validated upstream in `parse_proposals`); "" if absent.
    confidence: str = ""
    # Real candidate matches captured from `purpose_code_resolve` tool results; empty for
    # extraction proposals (invoiceNo/reference), which have no candidate trace.
    considered: list[dict] = []


class ProposeResponse(BaseModel):
    paymentId: str
    proposals: list[Proposal] = []


class InvestigateRequest(BaseModel):
    exceptionId: str
    paymentId: str


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "enrichmentAgentReady": ENRICHMENT_AGENT is not None,
        "reconciliationAgentReady": RECON_AGENT is not None,
    }


@app.post("/enrichment/propose", response_model=ProposeResponse)
def enrichment_propose(req: ProposeRequest) -> ProposeResponse:
    if ENRICHMENT_AGENT is None:
        # 200 with empty proposals — the caller treats [] as "agent had nothing to add".
        logger.warning("/enrichment/propose called but agent not ready — returning [].")
        return ProposeResponse(paymentId=req.paymentId, proposals=[])
    proposals = propose(ENRICHMENT_AGENT, req.payment, req.paymentId)
    return ProposeResponse(
        paymentId=req.paymentId,
        proposals=[Proposal(**p) for p in proposals],
    )


@app.post("/reconciliation/investigate")
def reconciliation_investigate(req: InvestigateRequest) -> dict:
    """Manually trigger an investigation (the change-stream worker is the usual trigger)."""
    if RECON_AGENT is None or _DB is None:
        logger.warning("/reconciliation/investigate called but agent not ready — no-op.")
        return {"exceptionId": req.exceptionId, "agent": None}
    agent_block = investigate(RECON_AGENT, _DB, req.exceptionId, req.paymentId)
    return {"exceptionId": req.exceptionId, "agent": agent_block}


@app.get("/reconciliation/{exception_id}")
def get_investigation(exception_id: str) -> dict:
    """Read back the `exceptions.agent{}` subdoc the agent recorded (for the Operations UI)."""
    if _DB is None:
        return {"exceptionId": exception_id, "agent": None}
    doc = _DB["exceptions"].find_one({"exceptionId": exception_id}, {"_id": 0, "agent": 1})
    return {"exceptionId": exception_id, "agent": (doc or {}).get("agent")}


@app.post("/reconciliation/{exception_id}/approve")
def approve_investigation(exception_id: str) -> dict:
    """Resume the paused Reconciliation Agent graph (HITL approval gate).

    The agent's `approval` node called `interrupt()` to surface its investigation for
    operator review. This endpoint resumes the graph with the operator's acknowledgement.
    The operator then resolves the exception through the existing transactions UI
    (`POST /workflow/exceptions/{id}/resolve`) — this gate reviews, it does not execute,
    so there is no duplicate resolve path.
    """
    if RECON_AGENT is None:
        return {"exceptionId": exception_id, "approved": False, "error": "agent not ready"}
    from langgraph.types import Command

    try:
        RECON_AGENT.invoke(
            Command(resume=True),
            config={"configurable": {"thread_id": exception_id}},
        )
        return {"exceptionId": exception_id, "approved": True}
    except Exception:  # noqa: BLE001 — never 500 on a resume failure.
        logger.warning(
            "approve resume failed for %s — graph left paused.", exception_id, exc_info=True,
        )
        return {"exceptionId": exception_id, "approved": False, "error": "resume failed"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8004")))
