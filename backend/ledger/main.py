import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before any project imports so module-level os.getenv() calls in
# routers/workers see the values (import-time evaluation happens on first import).
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from database.connection import MongoDBConnection
from shared.coa_cache import ChartOfAccounts
from workers import gl_batch, ingest_worker, projection_worker

from routers.financial_accounting import router as fa_router
from routers.pipeline import router as pipeline_router
from routers.proxy import router as proxy_router

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

logger = logging.getLogger(__name__)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI environment variable is required")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
ENABLE_CHANGE_STREAMS = os.getenv("ENABLE_CHANGE_STREAMS", "true").lower() == "true"

# MongoClient is lazy — constructing it here does not require a live DB at boot.
connection = MongoDBConnection(MONGODB_URI)


def _restart_loop(name: str, fn, *args, restart_delay: int = 5) -> None:
    while True:
        try:
            fn(*args)
        except Exception:
            logger.exception("%s crashed; restarting in %ds", name, restart_delay)
            time.sleep(restart_delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    coa = ChartOfAccounts.from_db(connection, DB_NAME)
    logger.info("CoA loaded: %d accounts", len(coa))
    interval = int(os.getenv("GL_BATCH_INTERVAL_SECONDS", "600"))

    change_stream_workers = [
        ("ingest_worker",     ingest_worker.run,     (connection, DB_NAME, coa)),
        ("projection_worker", projection_worker.run, (connection, DB_NAME, coa)),
    ]
    batch_workers = [
        ("gl_batch",          gl_batch.run,          (connection, DB_NAME, coa, interval)),
    ]
    if ENABLE_CHANGE_STREAMS:
        for name, fn, args in change_stream_workers:
            threading.Thread(
                target=_restart_loop, args=(name, fn, *args),
                daemon=True, name=name,
            ).start()
            logger.info("started background worker: %s", name)
    else:
        logger.info("change streams disabled via ENABLE_CHANGE_STREAMS=false; skipping ingest and projection workers")
    for name, fn, args in batch_workers:
        threading.Thread(
            target=_restart_loop, args=(name, fn, *args),
            daemon=True, name=name,
        ).start()
        logger.info("started background worker: %s", name)

    app.state.connection = connection
    app.state.db_name = DB_NAME
    app.state.coa = coa
    yield


app = FastAPI(
    title="Leafy Bank — Ledger (BIAN FinancialAccounting / FinancialBookingLog)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(fa_router)
app.include_router(pipeline_router)
app.include_router(proxy_router)


@app.get("/gl-monitor", include_in_schema=False)
def gl_monitor():
    return FileResponse(os.path.join(_STATIC_DIR, "gl_monitor.html"))


@app.get("/")
async def read_root():
    return {
        "service": "leafy-bank-ledger",
        "bian": "FinancialAccounting / FinancialBookingLog",
        "db": DB_NAME,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}
