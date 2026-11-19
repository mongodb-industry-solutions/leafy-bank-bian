"""Live concurrency test: funds reservation cannot overdraw under racing requests.

The hermetic FakeCollection runs callbacks serially and never raises a WriteConflict, so it
cannot prove the write-skew guarantee. This test fires real concurrent reserves at a real
MongoDB and checks the invariant directly: the number of successes is exactly what the
balance allows, and `available` never goes negative.

Requires MONGODB_URI (skips otherwise). Runs in a throwaway database named per run and drops
its collections afterwards, so it never touches shared collections.

Run:
    MONGODB_URI="mongodb+srv://..." pytest tests/test_funds_reservation_concurrency.py -v
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from pymongo import MongoClient

from contexts.payment_order_initiation.application import funds_reservation

# Read, don't load: `load_dotenv` would push the whole .env into os.environ at collection time
# and change the behaviour of every other test in the session (auth flags, secrets).
MONGODB_URI = os.getenv("MONGODB_URI") or dotenv_values(
    os.path.join(os.path.dirname(__file__), "..", ".env")
).get("MONGODB_URI")

OPENING_BALANCE = 300.0
AMOUNT = 80.0
REQUESTS = 10
EXPECTED_WINNERS = int(OPENING_BALANCE // AMOUNT)  # 3


@pytest.fixture
def db():
    if not MONGODB_URI:
        pytest.skip("MONGODB_URI not set")
    client = MongoClient(MONGODB_URI)
    name = f"test_reserve_{uuid.uuid4().hex[:10]}"
    database = client[name]
    # Created up front: implicit collection creation inside a transaction is version-dependent.
    database.create_collection("accounts")
    database.create_collection("payments")
    yield database
    # Collection drops, not dropDatabase: the app's Atlas user is not granted dropDatabase.
    # An empty database disappears once its last collection is gone.
    for coll in database.list_collection_names():
        database.drop_collection(coll)
    client.close()


def _ctx(db, account_id: str, payment_id: str):
    return SimpleNamespace(
        collections=SimpleNamespace(db=db, payments=db["payments"], accounts=db["accounts"]),
        payment_id=payment_id,
        debtor_account_ref=account_id,
        instructed_amount=AMOUNT,
    )


def test_concurrent_reserves_never_overdraw(db):
    account_id = f"TEST-ACC-{uuid.uuid4().hex[:8]}"
    db["accounts"].insert_one({
        "accountId": account_id,
        "balance": {"current": OPENING_BALANCE, "available": OPENING_BALANCE,
                    "ledger": OPENING_BALANCE, "hold": 0.0},
    })
    payment_ids = [f"PAY-{uuid.uuid4().hex[:12]}" for _ in range(REQUESTS)]
    db["payments"].insert_many([
        {"paymentId": pid, "instructedAmount": AMOUNT, "refs": {}} for pid in payment_ids
    ])

    start = threading.Barrier(REQUESTS)

    def reserve(pid):
        start.wait()  # release every thread at once to maximise overlap
        return funds_reservation.reserve(_ctx(db, account_id, pid))

    with ThreadPoolExecutor(max_workers=REQUESTS) as pool:
        results = list(pool.map(reserve, payment_ids))

    winners = [r for r in results if r is not None]
    balance = db["accounts"].find_one({"accountId": account_id})["balance"]

    assert len(winners) == EXPECTED_WINNERS
    assert balance["available"] == pytest.approx(OPENING_BALANCE - EXPECTED_WINNERS * AMOUNT)
    assert balance["available"] >= 0
    assert balance["hold"] == pytest.approx(EXPECTED_WINNERS * AMOUNT)
    # Losers must leave no marker behind: the insufficient-funds raise rolls the marker back.
    marked = db["payments"].count_documents({funds_reservation.ID_FIELD: {"$ne": None}})
    assert marked == EXPECTED_WINNERS
