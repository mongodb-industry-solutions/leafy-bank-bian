"""Integration tests for the EOD top-up worker.

Requires a live MongoDB connection (MONGODB_URI env var). Tests insert isolated
documents with a unique prefix and clean up after themselves.

Run:
    MONGODB_URI="mongodb+srv://..." pytest tests/test_eod_topup_worker.py -v
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from database.connection import MongoDBConnection
from workers.eod_topup_worker import run_once

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

THRESHOLD = 500.0
AMOUNT = 500.0


@pytest.fixture(scope="module")
def accounts_coll():
    if not MONGODB_URI:
        pytest.skip("MONGODB_URI not set")
    conn = MongoDBConnection(MONGODB_URI)
    yield conn.get_collection(DB_NAME, "accounts")


@pytest.fixture(autouse=True)
def cleanup(accounts_coll, request):
    prefix = request.node.name
    yield
    accounts_coll.delete_many({"accountId": {"$regex": f"^TEST-{prefix}-"}})


def _make_account(prefix, suffix, account_type, available):
    return {
        "accountId": f"TEST-{prefix}-{suffix}-{uuid.uuid4().hex[:6]}",
        "accountNumber": f"TEST-{uuid.uuid4().hex[:8]}",
        "type": account_type,
        "status": "ACTIVE",
        "currency": "USD",
        "balance": {
            "current": available,
            "available": available,
            "ledger": available,
            "hold": 0,
            "overdraftLimit": 0,
        },
    }


def test_below_threshold_is_credited(accounts_coll, request):
    doc = _make_account(request.node.name, "below", "CURRENT", 100.0)
    accounts_coll.insert_one(doc)

    count = run_once(accounts_coll, THRESHOLD, AMOUNT)

    assert count >= 1
    updated = accounts_coll.find_one({"accountId": doc["accountId"]})
    assert updated["balance"]["available"] == pytest.approx(600.0)
    assert updated["balance"]["current"] == pytest.approx(600.0)


def test_above_threshold_not_credited(accounts_coll, request):
    doc = _make_account(request.node.name, "above", "CURRENT", 600.0)
    accounts_coll.insert_one(doc)

    run_once(accounts_coll, THRESHOLD, AMOUNT)

    unchanged = accounts_coll.find_one({"accountId": doc["accountId"]})
    assert unchanged["balance"]["available"] == pytest.approx(600.0)


def test_non_customer_account_types_excluded(accounts_coll, request):
    docs = [
        _make_account(request.node.name, "gl", "GL_ACCOUNT", 100.0),
        _make_account(request.node.name, "nostro", "NOSTRO", 100.0),
        _make_account(request.node.name, "vostro", "VOSTRO", 100.0),
    ]
    accounts_coll.insert_many(docs)

    run_once(accounts_coll, THRESHOLD, AMOUNT)

    for doc in docs:
        unchanged = accounts_coll.find_one({"accountId": doc["accountId"]})
        assert unchanged["balance"]["available"] == pytest.approx(100.0), (
            f"{doc['type']} account should not be topped up"
        )


def test_inactive_account_excluded(accounts_coll, request):
    doc = _make_account(request.node.name, "dormant", "CURRENT", 100.0)
    doc["status"] = "DORMANT"
    accounts_coll.insert_one(doc)

    run_once(accounts_coll, THRESHOLD, AMOUNT)

    unchanged = accounts_coll.find_one({"accountId": doc["accountId"]})
    assert unchanged["balance"]["available"] == pytest.approx(100.0)


def test_ledger_balance_not_modified(accounts_coll, request):
    """balance.ledger must not change — it is owned by the GL pipeline."""
    doc = _make_account(request.node.name, "ledger", "SAVINGS", 100.0)
    accounts_coll.insert_one(doc)

    run_once(accounts_coll, THRESHOLD, AMOUNT)

    updated = accounts_coll.find_one({"accountId": doc["accountId"]})
    assert updated["balance"]["ledger"] == pytest.approx(100.0), (
        "balance.ledger should remain unchanged — GL pipeline owns this field"
    )
    assert updated["balance"]["available"] == pytest.approx(600.0)


def test_savings_account_credited(accounts_coll, request):
    doc = _make_account(request.node.name, "savings", "SAVINGS", 200.0)
    accounts_coll.insert_one(doc)

    run_once(accounts_coll, THRESHOLD, AMOUNT)

    updated = accounts_coll.find_one({"accountId": doc["accountId"]})
    assert updated["balance"]["available"] == pytest.approx(700.0)
