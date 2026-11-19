"""Write-skew showcase (W1): three real $80 wires race for one $200 account.

Each wire goes through the real `PaymentsService.initiate_payment` (the same saga a customer's
wire takes), so the winners are debited at stage 5 and go on to settle and reconcile like any
other wire. A rendezvous just before stage 3's reserve (`before_reserve`) releases the three
into the guarded update together; without it the saga's earlier stages would stagger them and
they might never collide. The guard is the conditional `$inc` on the account document, so
exactly two reserve and the third is refused for insufficient funds.

## One persistent account

The race runs on `ACC-w1demo`, owned by `CUST-w1demo`. Both are upserted on first use and the
balance is reset to $200 at the start of every run, the way the EOD top-up worker resets a
balance: no transaction record. The account carries `sourceSystem: concurrency-demo-seed` so it
is recognisable, and it is a real customer account, so the winners' wires show up in Payments.

## What is recorded

The run's own `PaymentsService` sits on a dedicated client with a command listener. Sync pymongo
publishes command events on the calling thread, so a thread-local worker id attributes each
command to the wire that issued it. Only the commands that matter to the story are kept: the
balance changes on `accounts`, the reservation marker on `payments`, the `transactions` insert
and each transaction's commit or abort.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from pymongo import MongoClient, monitoring

from contexts.financial_gateway.application.recon_scenarios import _BANKS
from contexts.financial_gateway.application.statement import PRESENTER_DRIVEN_REF_PREFIX
from contexts.financial_gateway.domain import camt053
from contexts.payment_order_initiation.application import funds_reservation
from services.payments_service import PaymentsService

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "concurrency-demo-seed"
ACCOUNT_ID = "ACC-w1demo"
CUSTOMER_ID = "CUST-w1demo"
TEMPLATE_CUSTOMER_ID = "CUST-abc10001"  # a verified COMMERCIAL customer to clone
OPENING = 200.0
AMOUNT = 80.0
WORKERS = 3
BARRIER_TIMEOUT_SECONDS = 20
WRITE_CONFLICT = 112

RESERVED = "RESERVED"
REFUSED = "REFUSED_INSUFFICIENT_FUNDS"
ERROR = "ERROR"

# Three different beneficiaries, so duplicate detection never fires and the wires stay apart.
_CREDITORS = (
    ("GB", "GB29NWBK60161331926819", "Invoice settlement"),
    ("DE", "DE89370400440532013000", "Freight and handling"),
    ("CH", "CH9300762011623852957", "Consulting services"),
)

_RUN_LOCK = threading.Lock()


def _plain(value):
    """bson values (dates) to JSON-safe ones for the report."""
    return json.loads(json.dumps(value, default=str))


def _ms(clock: float) -> float:
    return round((time.perf_counter() - clock) * 1000, 1)


# --- the persistent demo account ------------------------------------------------------------

def ensure_demo_account(db) -> None:
    """Upsert the demo customer and account, then reset the balance to the opening amount."""
    now = datetime.now(timezone.utc)
    if db["customers"].find_one({"customerId": CUSTOMER_ID}) is None:
        template = db["customers"].find_one({"customerId": TEMPLATE_CUSTOMER_ID})
        if template is None:
            raise ValueError(f"Cannot create the demo customer: {TEMPLATE_CUSTOMER_ID} is missing.")
        customer = copy.deepcopy(template)
        customer.pop("_id", None)
        customer.update(customerId=CUSTOMER_ID, sourceSystem=SOURCE_SYSTEM, createdBy=SOURCE_SYSTEM)
        ident = customer.get("identification") or {}
        ident.update(userName="w1demo", legalName="Concurrency Demo Co", tradingName="Concurrency Demo")
        customer["identification"] = ident
        db["customers"].insert_one(customer)

    account = {
        "accountBank": "Leafy Bank", "accountId": ACCOUNT_ID,
        "accountIdentificationType": "AccountNumber", "accountNumber": "310099001",
        "branchId": "BRANCH-DEFAULT-001", "closedAt": None, "createdAt": now, "createdBy": SOURCE_SYSTEM,
        "currency": "USD", "customerSnapshot": {"customerId": CUSTOMER_ID},
        "description": "Concurrency demo operating account",
        "gl": {"accountCode": "2111", "costCenter": "CC-RETAIL-DEFAULT", "profitCenter": "PC-RETAIL-DEFAULT"},
        "linkedCardIds": [], "linkedMandateIds": [], "maturesAt": None, "openedAt": now.date().isoformat(),
        "productId": "PROD-COMM-CA-USD", "restrictions": [],
        "signatories": [{"customerId": CUSTOMER_ID, "type": "PRIMARY", "signingRule": "SOLE",
                         "addedAt": now.date().isoformat()}],
        "sourceSystem": SOURCE_SYSTEM, "type": "CURRENT", "version": 1,
    }
    db["accounts"].update_one({"accountId": ACCOUNT_ID}, {"$setOnInsert": account}, upsert=True)
    db["accounts"].update_one(
        {"accountId": ACCOUNT_ID},
        {"$set": {"status": "ACTIVE", "updatedAt": now,
                  "balance": {"current": OPENING, "available": OPENING, "ledger": OPENING, "hold": 0.0,
                              "overdraftLimit": 0, "updatedAt": now}}},
    )


# --- command recording ----------------------------------------------------------------------

class _CommandRecorder(monitoring.CommandListener):
    """Counts write conflicts per worker and records each worker's story-relevant commands."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self.counts: dict[int, int] = {}
        self.ops: dict[int, list[dict]] = {}
        self.clock = time.perf_counter()

    def bind(self, worker: int) -> None:
        self._local.worker = worker
        self._local.txn = 0
        self._local.txn_interesting = False

    @property
    def worker(self):
        return getattr(self._local, "worker", None)

    @staticmethod
    def _describe(name: str, cmd: dict):
        """(collection, filter, update) for a command worth showing, else None."""
        if name == "findAndModify":
            collection, update = cmd["findAndModify"], cmd.get("update", {})
            if collection == "accounts" or (collection == "payments" and "fundsReleasedAt" in str(update)):
                return collection, cmd.get("query", {}), update
        if name == "update":
            u = cmd["updates"][0]
            if cmd["update"] == "accounts":
                return "accounts", u["q"], u["u"]
            if cmd["update"] == "payments" and ("fundsReservation" in str(u["u"]) or "fundsReleasedAt" in str(u["u"])):
                return "payments", u["q"], u["u"]
        if name == "insert" and cmd["insert"] == "transactions":
            doc = cmd["documents"][0]
            return "transactions", {"paymentId": doc.get("paymentId")}, None
        return None

    def started(self, event) -> None:
        worker = self.worker
        if worker is None:
            return
        cmd = event.command
        if cmd.get("startTransaction"):
            self._local.txn += 1
            self._local.txn_interesting = False
        name = event.command_name
        op = {"command": name, "attempt": self._local.txn or None,
              "startsTransaction": bool(cmd.get("startTransaction")), "atMs": _ms(self.clock), "result": None}
        if name in ("commitTransaction", "abortTransaction"):
            if not self._local.txn_interesting:
                return
        else:
            described = self._describe(name, cmd)
            if described is None:
                return
            self._local.txn_interesting = True
            collection, flt, upd = described
            op.update(collection=collection, filter=_plain(flt))
            if upd is not None:
                op["update"] = _plain(upd)
        with self._lock:
            self.ops.setdefault(worker, []).append(op)
            self._pending[event.request_id] = op

    def succeeded(self, event) -> None:
        with self._lock:
            op = self._pending.pop(event.request_id, None)
        if op is None:
            return
        reply = event.reply
        if op["command"] == "findAndModify":
            op["result"] = "MATCHED" if reply.get("value") is not None else "NO_MATCH"
        elif op["command"] == "update":
            op["result"] = "MATCHED" if reply.get("n", 0) > 0 else "NO_MATCH"
        else:
            op["result"] = "OK"

    def failed(self, event) -> None:
        failure = event.failure if isinstance(event.failure, dict) else {}
        conflict = failure.get("code") == WRITE_CONFLICT or \
            "TransientTransactionError" in failure.get("errorLabels", [])
        with self._lock:
            op = self._pending.pop(event.request_id, None)
        if op is None:
            return
        if op["command"] == "abortTransaction":
            op["result"] = "ABORTED"
            return
        op["result"] = "WRITE_CONFLICT" if conflict else "ERROR"
        if conflict and self.worker is not None:
            with self._lock:
                self.counts[self.worker] = self.counts.get(self.worker, 0) + 1


# --- the race ---------------------------------------------------------------------------------

def run(base_service: PaymentsService, connection, db_name: str) -> dict:
    """Race three real wires on the demo account and report each one."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise ValueError("A concurrency run is already in progress. Wait for it to finish.")
    run_id = uuid.uuid4().hex[:8]
    counter = _CommandRecorder()
    client = MongoClient(connection.uri, event_listeners=[counter], maxPoolSize=WORKERS * 4 + 5)
    try:
        db = client[db_name]
        ensure_demo_account(db)
        service = PaymentsService(SimpleNamespace(get_database=lambda name: client[name]), db_name,
                                  base_service.payment_limit_usd)
        barrier = threading.Barrier(WORKERS)
        clock = time.perf_counter()
        counter.clock = clock
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            attempts = list(pool.map(
                lambda n: _attempt(service, db, counter, n, run_id, barrier, clock),
                range(1, WORKERS + 1),
            ))
        elapsed = _ms(clock)
        balance = db["accounts"].find_one({"accountId": ACCOUNT_ID})["balance"]
    finally:
        client.close()
        _RUN_LOCK.release()

    for attempt in attempts:
        attempt["writeConflicts"] = counter.counts.get(attempt["worker"], 0)
        attempt["ops"] = counter.ops.get(attempt["worker"], [])
    return {
        "runId": run_id,
        "accountId": ACCOUNT_ID,
        "invariant": "balance.available >= 0",
        "params": {"openingBalance": OPENING, "amount": AMOUNT, "workers": WORKERS},
        "guard": {
            "filter": funds_reservation.guarded_filter("<accountId>", "<amount>"),
            "update": {"$inc": {"balance.available": "-<amount>", "balance.hold": "<amount>"}},
        },
        "attempts": attempts,
        "summary": {
            "winners": sum(a["outcome"] == RESERVED for a in attempts),
            "refused": sum(a["outcome"] == REFUSED for a in attempts),
            "errors": sum(a["outcome"] == ERROR for a in attempts),
            "totalWriteConflicts": sum(a["writeConflicts"] for a in attempts),
            "finalAvailable": balance["available"],
            "finalHold": balance["hold"],
            "invariantHeld": balance["available"] >= 0 and balance["hold"] >= 0,
            "elapsedMs": elapsed,
        },
    }


def _attempt(service: PaymentsService, db, counter: _CommandRecorder, worker: int, run_id: str,
             barrier: threading.Barrier, clock: float) -> dict:
    counter.bind(worker)
    country, account_no, purpose = _CREDITORS[(worker - 1) % len(_CREDITORS)]
    bank = _BANKS[country]
    reference = f"{PRESENTER_DRIVEN_REF_PREFIX}W1-{run_id}-{worker}"
    attempt = {"worker": worker, "paymentId": None, "status": None, "observedAvailable": None,
               "outcome": ERROR, "writeConflicts": 0, "readAtMs": None, "writeAtMs": None,
               "doneAtMs": None, "error": None}

    def rendezvous(_ctx) -> None:
        # Display only: every wire sees the same balance here. The guard does not trust this read.
        account = db["accounts"].find_one({"accountId": ACCOUNT_ID}, {"balance.available": 1})
        attempt["observedAvailable"] = account["balance"]["available"]
        attempt["readAtMs"] = _ms(clock)
        try:
            barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        except threading.BrokenBarrierError:
            pass  # a sibling failed before reaching here; carry on unsynchronised
        attempt["writeAtMs"] = _ms(clock)

    try:
        service.initiate_payment(
            customer_ref=CUSTOMER_ID,
            debtor_account_ref=ACCOUNT_ID,
            creditor_account_ref=None,
            creditor_party={"name": bank["name"], "accountNo": account_no, "bic": bank["bic"],
                            "bankName": bank["bankName"], "bankCountry": country,
                            "address": bank["address"],
                            "clearingSystemCode": bank["clearingSystemCode"],
                            "clearingSystemMemberId": bank["clearingSystemMemberId"]},
            instructed_amount=AMOUNT,
            instructed_currency="USD",
            payment_type="CREDIT_TRANSFER",
            payment_rail="WIRE",
            remittance_unstructured=purpose,
            charge_bearer="SHAR",
            channel="BRANCH",
            client_reference=reference,
            statement_outcome=camt053.CLEAN,
            before_reserve=rendezvous,
        )
        attempt["outcome"] = RESERVED
    except ValueError as e:
        if "Insufficient available balance" in str(e):
            attempt["outcome"] = REFUSED
        else:
            attempt["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - reported per payment, not as a 500
        logger.exception("concurrency demo worker %s failed", worker)
        attempt["error"] = str(e)
        barrier.abort()
    attempt["doneAtMs"] = _ms(clock)
    payment = db["payments"].find_one({"clientReference": reference}, {"paymentId": 1, "status": 1})
    if payment:
        attempt.update(paymentId=payment["paymentId"], status=payment.get("status"))
    return attempt
