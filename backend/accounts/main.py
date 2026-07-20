import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from api_models import (
    AccountActivityRequestRequest,
    AccountControlRequest,
    AccountInitiateRequest,
    AccountRequestRequest,
    PartyReferenceRequestRequest,
)
from bian.api_catalog import API_CATALOG
from database.connection import MongoDBConnection
from encoder.json_encoder import MyJSONEncoder
from services.accounts_service import AccountsService
from services.bian_service import BianService
from services.customers_service import CustomersService
from shared import registry
from workers import eod_topup_worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

logger = logging.getLogger(__name__)

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
connection = MongoDBConnection(MONGODB_URI)


def _restart_loop(name: str, fn, *args, restart_delay: int = 5) -> None:
    while True:
        try:
            fn(*args)
        except Exception:
            logger.exception("%s crashed; restarting in %ds", name, restart_delay)
            time.sleep(restart_delay)
            continue
        # fn returned without raising: it exited on purpose (e.g. worker
        # disabled). Do NOT re-invoke — that would busy-loop with no sleep and
        # flood the logs. Stop the thread cleanly.
        logger.info("%s exited; not restarting", name)
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    threshold = float(os.getenv("EOD_TOPUP_THRESHOLD", "500"))
    amount = float(os.getenv("EOD_TOPUP_AMOUNT", "500"))
    interval = int(os.getenv("EOD_TOPUP_INTERVAL_SECONDS", "0"))
    threading.Thread(
        target=_restart_loop,
        args=("eod_topup_worker", eod_topup_worker.run, connection, DB_NAME, threshold, amount, interval),
        daemon=True,
        name="eod-topup-worker",
    ).start()
    logger.info("started background worker: eod_topup_worker")
    yield


app = FastAPI(
    title="Leafy Bank — Accounts (BIAN PartyReferenceDataDirectory + CurrentAccount)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

accounts_service = AccountsService(connection, DB_NAME)
customers_service = CustomersService(connection, DB_NAME)
bian_service = BianService(connection, DB_NAME, "bian-mapping")


def _bian_response(envelope: dict) -> Response:
    return Response(
        content=json.dumps(envelope, cls=MyJSONEncoder),
        media_type="application/json",
    )


def _strip(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


# ---------- Health / root ----------

@app.get("/")
async def read_root():
    return {
        "service": "leafy-bank-accounts",
        "bian": [
            "PartyReferenceDataDirectory",
            "CurrentAccount",
        ],
        "bianVersion": registry.bian_version,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.post("/accounts/topup/run")
async def topup_run():
    threshold = float(os.getenv("EOD_TOPUP_THRESHOLD", "500"))
    amount = float(os.getenv("EOD_TOPUP_AMOUNT", "500"))
    accounts = connection.get_collection(DB_NAME, "accounts")
    credited = eod_topup_worker.run_once(accounts, threshold, amount)
    return {"credited": credited, "threshold": threshold, "amount": amount}


# ---------- PartyReferenceDataDirectory ----------

@app.get("/PartyReferenceDataDirectory/{partyreferencedatadirectoryid}/Retrieve")
async def party_retrieve(partyreferencedatadirectoryid: str):
    try:
        customer = customers_service.get_customer(partyreferencedatadirectoryid)
        if not customer:
            raise HTTPException(status_code=404, detail="customerId not found.")
        return _bian_response({
            "customerId": customer["customerId"],
            "customer": _strip(customer),
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PartyReferenceDataDirectory/{id}/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


@app.post("/PartyReferenceDataDirectory/Request")
async def party_request(body: PartyReferenceRequestRequest):
    try:
        customers = customers_service.list_customers(body.model_dump(exclude_none=True))
        return _bian_response({
            "customers": [_strip(c) for c in customers],
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PartyReferenceDataDirectory/Request failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal list error.")


@app.get("/PartyReferenceDataDirectory/{partyreferencedatadirectoryid}/CustomerKYCRecord/Retrieve")
async def party_kyc_retrieve(partyreferencedatadirectoryid: str):
    try:
        doc = customers_service.get_customer_kyc(partyreferencedatadirectoryid)
        if not doc:
            raise HTTPException(status_code=404, detail="customerId not found.")
        return _bian_response({
            "customerId": doc["customerId"],
            "kyc": doc.get("kyc", {}),
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PartyReferenceDataDirectory/{id}/CustomerKYCRecord/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal KYC retrieve error.")


# ---------- CurrentAccount ----------

@app.post("/CurrentAccount/Initiate")
async def account_initiate(body: AccountInitiateRequest):
    try:
        account_doc = accounts_service.create_account(
            customer_ref=body.customerId,
            product_ref=body.productId,
            account_number=body.accountNumber,
            currency=body.currency,
            account_type=body.type,
            initial_deposit=body.initialDeposit,
        )
        return _bian_response({
            "accountId": account_doc["accountId"],
            "account": _strip(account_doc),
        })
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error("CurrentAccount/Initiate failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal initiate error.")


@app.get("/CurrentAccount/{currentaccountid}/Retrieve")
async def account_retrieve(currentaccountid: str):
    try:
        account = accounts_service.get_account(currentaccountid)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")
        return _bian_response({
            "accountId": account["accountId"],
            "account": _strip(account),
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("CurrentAccount/{id}/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


@app.post("/CurrentAccount/Request")
async def account_request(body: AccountRequestRequest):
    try:
        accounts = accounts_service.list_accounts(body.model_dump(exclude_none=True))
        return _bian_response({
            "accounts": [_strip(a) for a in accounts],
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("CurrentAccount/Request failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal list error.")


@app.post("/CurrentAccount/Control")
async def account_control(body: AccountControlRequest):
    try:
        account = accounts_service.control_close(body.accountId, body.controlReason)
        return _bian_response({
            "accountId": account["accountId"],
            "controlAction": body.controlAction,
            "account": _strip(account),
        })
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error("CurrentAccount/Control failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal control error.")


@app.get("/CurrentAccount/{currentaccountid}/CurrentAccountBalanceRecord/Retrieve")
async def account_balance_retrieve(currentaccountid: str):
    try:
        doc = accounts_service.get_balance(currentaccountid)
        if not doc:
            raise HTTPException(status_code=404, detail="Account not found.")
        return _bian_response({
            "accountId": doc["accountId"],
            "balance": doc.get("balance", {}),
            "currency": doc.get("currency"),
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            "CurrentAccount/{id}/CurrentAccountBalanceRecord/Retrieve failed: %s",
            e,
        )
        raise HTTPException(status_code=500, detail="Internal balance retrieve error.")


@app.post("/CurrentAccount/CurrentAccountTransaction/Request")
async def account_activity_request(body: AccountActivityRequestRequest):
    try:
        legs = accounts_service.get_recent_activity(
            account_ref=body.accountId,
            customer_ref=body.customerId,
            limit=body.limit,
        )
        envelope = {"transactions": [_strip(leg) for leg in legs]}
        if body.accountId:
            envelope["accountId"] = body.accountId
        else:
            envelope["customerId"] = body.customerId
        return _bian_response(envelope)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(
            "CurrentAccount/CurrentAccountTransaction/Request failed: %s", e
        )
        raise HTTPException(status_code=500, detail="Internal activity error.")


# ---------------------------------------------------------------------------
# BIAN explorer endpoints (read-only metadata for the UI BIAN modal)
# ---------------------------------------------------------------------------

@app.get("/fetch-bian-mapping")
async def fetch_bian_mapping():
    try:
        document = bian_service.get_mapping()
        if document is None:
            raise HTTPException(
                status_code=404, detail="BIAN mapping document not found")
        logging.info("Returning BIAN mapping document")
        return Response(
            content=json.dumps({"mapping": document}, cls=MyJSONEncoder),
            media_type="application/json")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error retrieving BIAN mapping: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/fetch-bian-api-catalog")
async def fetch_bian_api_catalog():
    try:
        logging.info("Returning BIAN API catalog")
        return Response(
            content=json.dumps({"catalog": API_CATALOG}, cls=MyJSONEncoder),
            media_type="application/json")
    except Exception as e:
        logging.error(f"Error retrieving BIAN API catalog: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
