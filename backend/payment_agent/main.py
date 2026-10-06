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
from datetime import datetime, timezone
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
import cutoff_agent
import cutoff_cases
import cutoff_worker

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RECON_AGENT: Optional[Any] = None
CUTOFF_AGENT: Optional[Any] = None
_DB: Optional[Any] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global RECON_AGENT, CUTOFF_AGENT, _DB
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
        CUTOFF_AGENT = cutoff_agent.build_cutoff_agent(bedrock_model(), db, checkpointer)
        logger.info("Cutoff agent built.")
        cutoff_worker.start_cutoff_worker(CUTOFF_AGENT, db)
    except Exception:  # noqa: BLE001
        logger.warning("Agents could not be built at startup — endpoints will 503/degrade.",
                       exc_info=True)
    yield
    RECON_AGENT = CUTOFF_AGENT = _DB = None


app = FastAPI(title="Leafy Bank Payment Agent", lifespan=lifespan)


class InvestigateRequest(BaseModel):
    exceptionId: str
    paymentId: str


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "reconciliationAgentReady": RECON_AGENT is not None,
        "cutoffAgentReady": CUTOFF_AGENT is not None,
    }


@app.post("/reconciliation/investigate")
def reconciliation_investigate(req: InvestigateRequest) -> dict:
    """Manually trigger an investigation (the change-stream worker is the usual trigger)."""
    if RECON_AGENT is None or _DB is None:
        logger.warning("/reconciliation/investigate called but agent not ready — no-op.")
        return {"exceptionId": req.exceptionId, "agent": None}
    agent_block = investigate(RECON_AGENT, _DB, req.exceptionId, req.paymentId)
    return {"exceptionId": req.exceptionId, "agent": agent_block}


class WakeRequest(BaseModel):
    paymentId: str


@app.post("/reconciliation/wake")
def wake_scheduled_rechecks(req: WakeRequest) -> dict:
    """New evidence arrived (e.g. a late statement): bring the agent's scheduled recheck for
    this payment forward to now, so the next sweep (<= RECON_AGENT_SWEEP_SECONDS) runs it
    instead of waiting out the policy delay. Touches only exceptions that already have a
    pending nextCheckAt; the agent still decides the outcome."""
    if _DB is None:
        raise HTTPException(status_code=503, detail="Reconciliation agent not ready.")
    res = _DB["exceptions"].update_many(
        {"paymentId": req.paymentId, "status": "OPEN", "agent.nextCheckAt": {"$ne": None}},
        {"$set": {"agent.nextCheckAt": datetime.now(timezone.utc)}})
    return {"paymentId": req.paymentId, "woken": res.modified_count}


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


# --- Cutoff Agent ------------------------------------------------------------------------

_CASE_FILTERS = ("status", "clockRunId", "paymentId")


@app.get("/cutoff/cases")
def list_cutoff_cases(status: Optional[str] = None, clockRunId: Optional[str] = None,
                      paymentId: Optional[str] = None, active: Optional[bool] = None) -> dict:
    """Cutoff cases, newest first (limit 50), for the Operations UI."""
    if _DB is None:
        raise HTTPException(status_code=503, detail="Cutoff agent not ready.")
    values = {"status": status, "clockRunId": clockRunId, "paymentId": paymentId}
    query: dict = {k: values[k] for k in _CASE_FILTERS if values[k] is not None}
    if active is not None:
        query["active"] = active
    docs = _DB[cutoff_cases.COLLECTION].find(query, {"_id": 0}).sort("updatedAt", -1).limit(50)
    return {"cases": list(docs)}


@app.get("/cutoff/cases/{case_id}")
def get_cutoff_case(case_id: str) -> dict:
    if _DB is None:
        raise HTTPException(status_code=503, detail="Cutoff agent not ready.")
    case = cutoff_cases.get(_DB, case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"No cutoff case {case_id}.")
    return {"case": case}


@app.get("/cutoff/cases/{case_id}/steps")
def get_cutoff_case_steps(case_id: str) -> dict:
    """The agent's thinking timeline for one case (thread_id = caseId). 404 before any run."""
    if CUTOFF_AGENT is None:
        raise HTTPException(status_code=503, detail="Cutoff agent not ready.")
    messages = thread_messages(CUTOFF_AGENT, case_id)
    if not messages:
        raise HTTPException(status_code=404, detail=f"No agent thread for {case_id}.")
    return {"caseId": case_id,
            "awaitingApproval": is_awaiting_approval(CUTOFF_AGENT, case_id),
            "steps": messages_to_steps(messages)}


@app.post("/cutoff/cases/{case_id}/approve")
def approve_cutoff_case(case_id: str, req: ApproveRequest) -> dict:
    """Resume a paused HOLD / EXPEDITE / DEFER proposal with the operator's decision.
    409 when nothing is paused on this case."""
    if CUTOFF_AGENT is None or _DB is None:
        raise HTTPException(status_code=503, detail="Cutoff agent not ready.")
    # The same claim and lease as the worker, so the stale-case closer or a second approve
    # cannot resume or close this thread at the same time.
    with cutoff_worker.case_lock(_DB, case_id) as held:
        if not held:
            raise HTTPException(status_code=409, detail=f"Case {case_id} is busy; try again.")
        if not is_awaiting_approval(CUTOFF_AGENT, case_id):
            raise HTTPException(status_code=409,
                                detail=f"No agent proposal is awaiting approval on {case_id}.")
        try:
            cutoff_agent.resume(CUTOFF_AGENT, _DB, case_id, req.decision, req.note, req.by)
        except Exception:  # noqa: BLE001
            logger.warning("cutoff approve resume failed for %s", case_id, exc_info=True)
            paused = is_awaiting_approval(CUTOFF_AGENT, case_id)
            detail = ("Resuming the agent failed; the proposal is still paused." if paused else
                      "Resuming the agent failed after the decision was taken; the proposal is "
                      "no longer paused. Check the case.")
            raise HTTPException(status_code=502, detail=detail)
    return {"caseId": case_id, "decision": req.decision, "case": cutoff_cases.get(_DB, case_id)}


class CutoffSweepRequest(BaseModel):
    paymentId: Optional[str] = None


@app.post("/cutoff/sweep")
def cutoff_sweep(req: Optional[CutoffSweepRequest] = None) -> dict:
    """Run one sweep now (optionally scoped to one payment) instead of waiting for the timer."""
    if CUTOFF_AGENT is None or _DB is None:
        raise HTTPException(status_code=503, detail="Cutoff agent not ready.")
    if not cutoff_worker.enabled():
        raise HTTPException(status_code=503, detail="Cutoff agent disabled (ENABLE_CUTOFF_AGENT).")
    return cutoff_worker.sweep_once(CUTOFF_AGENT, _DB, payment_id=req.paymentId if req else None,
                                    source=cutoff_cases.SOURCE_MANUAL)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8004")))
