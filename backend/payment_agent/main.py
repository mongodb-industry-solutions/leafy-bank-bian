"""Payment Agent service — Phase-1 Reconciliation Agent.

A LangGraph agent over Bedrock (Haiku), in its own service so the money-path transactions
service stays thin. (The stage-3 Enrichment Agent was retired 2026-09-29 — Doina struck it
from the agentic design; stage 3 is fully deterministic.)

- **Reconciliation Agent** — asynchronous. Watches `exceptions` for OPEN reconciliation
  exceptions (DISCREPANCY, MISSING, ORPHANED_SETTLEMENT), classifies the cause, rechecks on its
  own or proposes an action for approval. `POST /reconciliation/investigate` triggers a run
  manually; `GET /reconciliation/{exception_id}` reads `exceptions.agent{}`;
  `POST /reconciliation/{exception_id}/approve` resumes a paused proposal with the operator's
  decision, executes it through the owning service's route and verifies the outcome.

Uses a `MongoDBSaver` checkpointer (graph state persists in Atlas).

Run: ``uvicorn main:app --port 8004`` (or ``python -m main``).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from typing import Literal

from pydantic import BaseModel

from bedrock import bedrock_model
from database.connection import MongoDBConnection
from reconciliation_agent import (build_reconciliation_agent, investigate, is_awaiting_approval,
                                  messages_to_steps, resume, thread_messages)
from reconciliation_worker import start_reconciliation_worker

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RECON_AGENT: Optional[Any] = None
_DB: Optional[Any] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global RECON_AGENT, _DB
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (see backend/payment_agent/.env).")
    db_name = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    try:
        connection = MongoDBConnection(uri)
        db = connection.get_database(db_name)
        from langgraph.checkpoint.mongodb import MongoDBSaver
        checkpointer = MongoDBSaver(connection.client)
        RECON_AGENT = build_reconciliation_agent(bedrock_model(), db, checkpointer)
        _DB = db
        logger.info("Reconciliation agent built.")
        start_reconciliation_worker(RECON_AGENT, db)
    except Exception:  # noqa: BLE001
        logger.warning("Reconciliation agent could not be built at startup — endpoints will 503/degrade.",
                       exc_info=True)
    yield
    RECON_AGENT = _DB = None


app = FastAPI(title="Leafy Bank Payment Agent", lifespan=lifespan)


class InvestigateRequest(BaseModel):
    exceptionId: str
    paymentId: str


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "reconciliationAgentReady": RECON_AGENT is not None,
    }


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


@app.get("/reconciliation/{exception_id}/steps")
def get_investigation_steps(exception_id: str) -> dict:
    """The agent's thinking timeline for one exception, read from its checkpointed thread.
    Read-only; the walkthrough polls it while the agent investigates. 404 before any run."""
    if RECON_AGENT is None:
        raise HTTPException(status_code=503, detail="Reconciliation agent not ready.")
    messages = thread_messages(RECON_AGENT, exception_id)
    if not messages:
        raise HTTPException(status_code=404, detail=f"No agent thread for {exception_id}.")
    return {"exceptionId": exception_id,
            "awaitingApproval": is_awaiting_approval(RECON_AGENT, exception_id),
            "steps": messages_to_steps(messages)}


class ApproveRequest(BaseModel):
    # Required, no default: APPROVE executes the proposal (possibly a money-moving
    # POST_ADJUSTMENT), so a bodyless call — e.g. the pre-Part-C "Acknowledge" button — must
    # not be read as an approval.
    decision: Literal["APPROVE", "REJECT"]
    note: Optional[str] = None
    by: str = "operator"


@app.post("/reconciliation/{exception_id}/approve")
def approve_investigation(exception_id: str, req: ApproveRequest) -> dict:
    """Resume a proposal paused at the approval gate with the operator's decision.

    APPROVE executes the proposed action through the transactions/ledger route and verifies
    the result; REJECT records the decision and ends the run. Returns `exceptions.agent{}`
    as it stands afterwards. 409 when no proposal is awaiting approval on this exception.
    """
    if RECON_AGENT is None or _DB is None:
        raise HTTPException(status_code=503, detail="Reconciliation agent not ready.")
    if not is_awaiting_approval(RECON_AGENT, exception_id):
        raise HTTPException(status_code=409,
                            detail=f"No agent proposal is awaiting approval on {exception_id}.")
    try:
        agent_block = resume(RECON_AGENT, _DB, exception_id, req.decision, req.note, req.by)
    except Exception:  # noqa: BLE001
        logger.warning("approve resume failed for %s", exception_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Resuming the agent failed; the proposal is still paused.")
    return {"exceptionId": exception_id, "decision": req.decision, "agent": agent_block}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8004")))
