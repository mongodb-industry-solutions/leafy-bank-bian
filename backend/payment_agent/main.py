"""Payment Agent service — Phase-1 Reconciliation Agent.

A LangGraph agent over Bedrock (Haiku), in its own service so the money-path transactions
service stays thin. (The stage-3 Enrichment Agent was retired 2026-09-29 — Doina struck it
from the agentic design; stage 3 is fully deterministic.)

- **Reconciliation Agent** — asynchronous, change-stream-driven. Watches `exceptions` for OPEN
  `RECONCILIATION_DISCREPANCY` inserts, investigates the mismatch, writes findings onto
  `exceptions.agent{}`. `POST /reconciliation/investigate` triggers a run manually;
  `GET /reconciliation/{exception_id}` reads the recorded investigation.

Uses a `MongoDBSaver` checkpointer (graph state persists in Atlas).

Run: ``uvicorn main:app --port 8004`` (or ``python -m main``).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from bedrock import bedrock_model
from database.connection import MongoDBConnection
from reconciliation_agent import build_reconciliation_agent, investigate
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
        start_reconciliation_worker(RECON_AGENT, db, connection)
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
