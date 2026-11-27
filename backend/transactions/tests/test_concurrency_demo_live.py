"""Live W1 run: three real wires race the persistent demo account through the saga.

Requires MONGODB_URI (skips otherwise). Uses the app's database, as the endpoint does. The run
upserts `ACC-acc10004` (owned by `CUST-abc10001`), resets the balance to $200, and leaves the wires in
Payments, so this asserts outcomes only and cleans nothing up.
"""

import os

import pytest
from dotenv import dotenv_values

from contexts.payment_order_initiation.application import concurrency_demo
from database.connection import MongoDBConnection
from services.payments_service import PaymentsService

# Read, don't load: `load_dotenv` would leak the .env into every other test's os.environ.
_ENV = dotenv_values(os.path.join(os.path.dirname(__file__), "..", ".env"))
MONGODB_URI = os.getenv("MONGODB_URI") or _ENV.get("MONGODB_URI")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME") or _ENV.get("LEAFYBANK_DB_NAME") or "leafy_bank_bian"


def test_three_racing_wires_leave_two_reserved_and_one_refused():
    if not MONGODB_URI:
        pytest.skip("MONGODB_URI not set")

    connection = MongoDBConnection(MONGODB_URI)
    service = PaymentsService(connection, DB_NAME, 1_000_000.0)
    report = concurrency_demo.run(service, connection, DB_NAME)

    summary = report["summary"]
    assert summary["winners"] == 2
    assert summary["refused"] == 1
    assert summary["errors"] == 0
    # The refused wire depends on the race: 80 + 110, 80 + 90 or 110 + 90 got through.
    refused = next(a for a in report["attempts"] if a["outcome"] == "REFUSED_INSUFFICIENT_FUNDS")
    winners_total = sum(a["amount"] for a in report["attempts"] if a["outcome"] == "RESERVED")
    assert summary["finalAvailable"] == pytest.approx(200.0 - winners_total)
    assert summary["finalAvailable"] < refused["amount"]
    assert summary["finalHold"] == pytest.approx(0.0)
    assert summary["invariantHeld"] is True
    assert all(a["observedAvailable"] == pytest.approx(200.0) for a in report["attempts"])
    assert all(a["paymentId"] for a in report["attempts"])

    # Every attempt reports the guarded update it ran against the account document.
    for a in report["attempts"]:
        guarded = [op for op in a["ops"] if op["command"] == "findAndModify" and op["collection"] == "accounts"
                   and "balance.available" in op["filter"]]
        assert guarded and guarded[-1]["filter"]["balance.available"] == {"$gte": a["amount"]}
