"""Characterisation tests for `PaymentsService.initiate_payment`.

These pin down the behaviour that exists TODAY, before the lifecycle state machine replaces
the `PENDING -> SETTLED` status write inside the ACID block. They are a tripwire, not a spec:
if a change here starts failing, the change moved money differently than the current code does.

Hermetic — no Atlas, no pymongo connection. Fake collections stand in for the five collections
the service touches, and a fake session runs the `with_transaction` callback inline. Follows the
convention in backend/ledger/tests/ (inline fixtures, no DB).

Why these four cases: each is a guard that protects real money.
  * insufficient funds     — the balance.available floor, checked twice (pre-flight and in-txn)
  * currency mismatch      — FX is out of scope; a silent pass would move the wrong value
  * closed account         — a debit against a closed account
  * duplicate endToEndId   — idempotency, enforced by the unique index, not the pre-check
Plus the happy path, so the tests fail loudly if the money move itself breaks.
"""

import copy
from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from services.payments_service import PaymentsService


# --- fakes --------------------------------------------------------------------

class FakeCollection:
    """In-memory stand-in supporting only what the service calls."""

    def __init__(self, docs=None, key="accountId"):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]
        self.key = key
        self.unique_on = None          # set to a field name to emulate a unique index
        self.inserted = []

    # -- reads
    def find_one(self, flt, *a, **kw):
        for d in self.docs:
            if self._matches(d, flt):
                return copy.deepcopy(d)
        return None

    # -- writes
    def insert_one(self, doc, *a, **kw):
        if self.unique_on:
            val = doc.get(self.unique_on)
            if any(d.get(self.unique_on) == val for d in self.docs):
                raise DuplicateKeyError(f"dup {self.unique_on}={val}")
        self.docs.append(copy.deepcopy(doc))
        self.inserted.append(copy.deepcopy(doc))
        return type("R", (), {"inserted_id": doc.get("_id")})()

    def insert_many(self, docs, *a, **kw):
        for d in docs:
            self.insert_one(d)
        return type("R", (), {"inserted_ids": [d.get("_id") for d in docs]})()

    def update_one(self, flt, update, *a, **kw):
        for d in self.docs:
            if self._matches(d, flt):
                self._apply(d, update)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    def find_one_and_update(self, flt, update, *a, **kw):
        for d in self.docs:
            if self._matches(d, flt):
                self._apply(d, update)
                return copy.deepcopy(d)
        return None          # the guard failing is signalled by None, as pymongo does

    # -- matching / mutation
    def _matches(self, doc, flt):
        for k, v in flt.items():
            actual = self._get(doc, k)
            if isinstance(v, dict):
                if "$gte" in v and not (actual is not None and actual >= v["$gte"]):
                    return False
                if "$ne" in v and actual == v["$ne"]:
                    return False
            elif actual != v:
                return False
        return True

    @staticmethod
    def _get(doc, dotted):
        cur = doc
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    @staticmethod
    def _set(doc, dotted, val):
        cur = doc
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = val

    def _apply(self, doc, update):
        for field, delta in update.get("$inc", {}).items():
            self._set(doc, field, (self._get(doc, field) or 0) + delta)
        for field, val in update.get("$set", {}).items():
            self._set(doc, field, val)


class FakeSession:
    """`with_transaction` runs the callback inline.

    NOTE: there is no rollback. These tests assert the guards that prevent the callback
    being entered at all; they cannot prove atomicity of a partial failure inside it.
    That needs a real replica set and belongs in an integration test.
    """

    def with_transaction(self, callback):
        return callback(self)

    # context-manager protocol, for `with client.start_session() as session:`
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClient:
    def start_session(self):
        return FakeSession()


class FakeDb(dict):
    """Dict of collections, plus the `.client` the service reaches through for sessions."""

    def __init__(self, mapping):
        super().__init__(mapping)
        self.client = FakeClient()


class FakeConnection:
    def __init__(self, db):
        self._db = db

    def get_database(self, _name):
        return self._db


# --- fixtures -----------------------------------------------------------------

def _account(account_id, customer_id, *, currency="USD", available=10_000.0,
             status="ACTIVE", account_type="CHECKING"):
    return {
        "accountId": account_id,
        "accountNumber": "8282993" + account_id[-2:],
        "status": status,
        "currency": currency,
        "accountType": account_type,
        "customerSnapshot": {"customerId": customer_id, "name": f"Holder {customer_id}"},
        "balance": {"current": available, "available": available, "ledger": available,
                    "updatedAt": datetime.now(timezone.utc)},
    }


def _customer(customer_id):
    return {"customerId": customer_id, "name": f"Holder {customer_id}"}


DEBTOR, CREDITOR = "ACC-debtor01", "ACC-credit01"
CUST_D, CUST_C = "CUST-0000000001", "CUST-0000000002"


@pytest.fixture
def db():
    """Two open USD accounts with 10,000 each, owned by different customers."""
    return FakeDb({
        "accounts": FakeCollection([_account(DEBTOR, CUST_D), _account(CREDITOR, CUST_C)]),
        "customers": FakeCollection([_customer(CUST_D), _customer(CUST_C)], key="customerId"),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
    })


@pytest.fixture
def service(db):
    db["payments"].unique_on = "endToEndId"      # mirrors idx_end_to_end_id_unique
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)


def _initiate(svc, **over):
    kwargs = dict(
        customer_ref=CUST_D,
        debtor_account_ref=DEBTOR,
        creditor_account_ref=CREDITOR,
        instructed_amount=250.0,
        instructed_currency="USD",
        payment_type="CREDIT_TRANSFER",
        payment_rail="WIRE",
        remittance_unstructured="INV-48392",
    )
    kwargs.update(over)
    return svc.initiate_payment(**kwargs)


# --- 1. insufficient funds ----------------------------------------------------

def test_insufficient_funds_rejected_and_no_money_moves(service, db):
    """The pre-flight balance.available floor. Nothing may be written when it trips."""
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(service, instructed_amount=10_000.01)

    assert db["payments"].inserted == [], "no payment order may be recorded"
    assert db["transactions"].inserted == [], "no ledger leg may be recorded"
    for acc in db["accounts"].docs:
        assert acc["balance"]["available"] == 10_000.0, "balances must be untouched"


def test_exact_available_balance_is_allowed(service, db):
    """Boundary: available == amount passes. `<` is the guard, not `<=`."""
    _initiate(service, instructed_amount=10_000.0)
    debtor = db["accounts"].find_one({"accountId": DEBTOR})
    assert debtor["balance"]["available"] == 0.0


# --- 2. currency mismatch -----------------------------------------------------

@pytest.mark.parametrize(
    "debtor_ccy,creditor_ccy,instructed",
    [
        ("USD", "EUR", "USD"),   # accounts disagree
        ("EUR", "EUR", "USD"),   # accounts agree, instruction disagrees
        ("USD", "USD", "EUR"),   # instruction disagrees the other way
    ],
)
def test_currency_mismatch_rejected(db, debtor_ccy, creditor_ccy, instructed):
    """FX is out of scope for Phase 1; all three disagreement shapes must reject."""
    db["accounts"] = FakeCollection([
        _account(DEBTOR, CUST_D, currency=debtor_ccy),
        _account(CREDITOR, CUST_C, currency=creditor_ccy),
    ])
    db["payments"].unique_on = "endToEndId"
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)

    with pytest.raises(ValueError, match="Currency mismatch"):
        _initiate(svc, instructed_currency=instructed)

    assert db["payments"].inserted == []


# --- 3. closed account --------------------------------------------------------

@pytest.mark.parametrize("side", ["debtor", "creditor"])
def test_closed_account_rejected(db, side):
    """A CLOSED account on either side stops the payment before anything is written."""
    accounts = [_account(DEBTOR, CUST_D), _account(CREDITOR, CUST_C)]
    accounts[0 if side == "debtor" else 1]["status"] = "CLOSED"
    db["accounts"] = FakeCollection(accounts)
    db["payments"].unique_on = "endToEndId"
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)

    with pytest.raises(ValueError, match="CLOSED"):
        _initiate(svc)

    assert db["payments"].inserted == []
    for acc in db["accounts"].docs:
        assert acc["balance"]["available"] == 10_000.0


# --- 4. duplicate endToEndId (idempotency) ------------------------------------

def test_duplicate_end_to_end_id_returns_first_payment_once(service, db):
    """Two identical requests produce ONE payment and move money ONCE.

    The pre-check short-circuits the second call. The unique index is the real enforcement —
    covered by the next test.
    """
    first = _initiate(service, idempotency_key="E2E-fixed-key")
    second = _initiate(service, idempotency_key="E2E-fixed-key")

    assert first["paymentId"] == second["paymentId"]
    assert len(db["payments"].inserted) == 1, "exactly one payment order"
    assert len(db["transactions"].inserted) == 1, "money moved exactly once"
    debtor = db["accounts"].find_one({"accountId": DEBTOR})
    assert debtor["balance"]["available"] == 9_750.0, "debited once, not twice"


def test_concurrent_duplicate_loses_race_and_returns_the_winner(service, db, monkeypatch):
    """The unique-index path: pre-check passes, insert loses, winner's doc is returned.

    This is the branch the `find_one` pre-check can never reach, and the one that actually
    makes idempotency true. Simulated by seeding the winner directly into `payments`,
    bypassing the pre-check, so `insert_one` raises DuplicateKeyError.
    """
    winner = {"_id": "oid-winner", "paymentId": "PAY-winner1",
              "endToEndId": "E2E-raced", "status": "SETTLED"}

    real_find_one = db["payments"].find_one
    calls = {"n": 0}

    def find_one_hiding_the_winner(flt, *a, **kw):
        # First call is the pre-check: pretend the winner is not there yet.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find_one(flt, *a, **kw)

    db["payments"].docs.append(winner)
    monkeypatch.setattr(db["payments"], "find_one", find_one_hiding_the_winner)

    result = _initiate(service, idempotency_key="E2E-raced")

    assert result["paymentId"] == "PAY-winner1", "the loser must see the winner's payment"
    assert db["transactions"].inserted == [], "the loser must not move money"
    debtor = db["accounts"].find_one({"accountId": DEBTOR})
    assert debtor["balance"]["available"] == 10_000.0


# --- happy path, so a break in the money move is loud -------------------------

def test_happy_path_moves_money_once_and_settles(service, db):
    payment = _initiate(service, instructed_amount=250.0)

    assert payment["status"] == "SETTLED"
    assert payment["paymentId"].startswith("PAY-")

    debtor = db["accounts"].find_one({"accountId": DEBTOR})
    creditor = db["accounts"].find_one({"accountId": CREDITOR})
    assert debtor["balance"]["available"] == 9_750.0
    assert creditor["balance"]["available"] == 10_250.0
    assert debtor["balance"]["current"] == 9_750.0
    assert debtor["balance"]["ledger"] == 9_750.0

    assert len(db["transactions"].inserted) == 1
    txn = db["transactions"].inserted[0]
    assert txn["direction"] == "OUTGOING"
    assert "legs" not in txn, "v4_21 one-doc shape: no debit/credit legs"
    assert "gl" not in txn, "accounting stays out of the payments domain"

    assert len(db["notifications"].inserted) == 1, "sender-only notification"


def test_self_transfer_rejected(service, db):
    with pytest.raises(ValueError, match="must differ"):
        _initiate(service, creditor_account_ref=DEBTOR)
    assert db["payments"].inserted == []


def test_debtor_account_not_owned_by_customer_rejected(service, db):
    with pytest.raises(ValueError, match="not owned by"):
        _initiate(service, customer_ref=CUST_C)
    assert db["payments"].inserted == []


def test_amount_over_limit_rejected(service, db):
    with pytest.raises(ValueError, match="exceeds the limit"):
        _initiate(service, instructed_amount=50_000.01)
    assert db["payments"].inserted == []


@pytest.mark.parametrize("amount", [0.0, -1.0])
def test_non_positive_amount_rejected(service, db, amount):
    with pytest.raises(ValueError, match="greater than 0"):
        _initiate(service, instructed_amount=amount)
    assert db["payments"].inserted == []
