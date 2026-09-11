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

from contexts.fraud_evaluation.domain import fraud_rules
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

    def find(self, flt=None, *a, **kw):
        """Added for stage 4: `fraud_rules` counts prior payments, and a fake that cannot
        answer a multi-document read would make every velocity and beneficiary-novelty
        assertion vacuous — the 2026-08-31 lesson about grading a consumer against a
        fixture no producer could produce."""
        return [copy.deepcopy(d) for d in self.docs if self._matches(d, flt or {})]

    def count_documents(self, flt=None, *a, **kw):
        return len(self.find(flt))

    # -- writes
    def insert_one(self, doc, *a, **kw):
        if self.unique_on:
            val = doc.get(self.unique_on)
            # SPARSE: a null carries no uniqueness, matching idx_idempotency_key_unique.
            if val is not None and any(d.get(self.unique_on) == val for d in self.docs):
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
                if "$nin" in v and actual in v["$nin"]:
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
        # $push backs lifecycle.events (one at a time) and payments.checks[] (a whole
        # stage's worth via $each). No $slice — both arrays are append-only.
        for field, val in update.get("$push", {}).items():
            existing = self._get(doc, field)
            if existing is None:
                existing = []
                self._set(doc, field, existing)
            if isinstance(val, dict) and "$each" in val:
                existing.extend(val["$each"])
            else:
                existing.append(val)


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
    """Dict of collections, plus the `.client` the service reaches through for sessions.

    `__missing__` mirrors pymongo: `db["anything"]` always yields a handle and never
    raises, so a collection no test seeds reads as empty rather than as a KeyError. Without
    it, adding any new collection to the service (stage 3's reference data was the first)
    breaks every test here for a reason that has nothing to do with the test.
    """

    def __init__(self, mapping):
        super().__init__(mapping)
        self.client = FakeClient()

    def __missing__(self, name):
        collection = FakeCollection([])
        self[name] = collection
        return collection


class FakeConnection:
    def __init__(self, db):
        self._db = db

    def get_database(self, _name):
        return self._db


# --- fixtures -----------------------------------------------------------------

# mod-97-valid IBANs, one per fixture account. Keyed on the full account id: DEBTOR and
# CREDITOR both END in "01", so keying on a suffix would hand two accounts the same IBAN.
# Every value is asserted valid by `test_fixture_ibans_are_mod97_valid` in
# test_stage_three_validation.py — a fixture identifier that fails the rule under test would
# make the whole suite meaningless.
_IBANS = {
    "ACC-debtor01": "GB33BUKB20201555555555",
    "ACC-credit01": "DE75512108001245126199",
}
_DEFAULT_IBAN = "GB82WEST12345698765432"


def _account(account_id, customer_id, *, currency="USD", available=10_000.0,
             status="ACTIVE", account_type="CHECKING", signing_rule="SOLE",
             signatories=None, restrictions=None):
    """One account, shaped as production writes it.

    `signatories[]` and `restrictions[]` are here because stage 2 reads both, and a fixture
    that omits a field the code reads tests a shape production never produces — the same
    fidelity failure as the `accountType` vs `type` slip noted below (doc 15 B7).
    """
    return {
        "accountId": account_id,
        "accountNumber": "8282993" + account_id[-2:],
        # `party_snapshot` copies this onto the payment, and stage 3 validates its mod-97
        # check digits (R8). The fixture omitted it, so every snapshot got `iban: None` and
        # the enrichment/format paths were never exercised — doc 17 §4's fixture-fidelity
        # item. A REAL mod-97-valid IBAN, so a test asserting acceptance means something.
        "iban": _IBANS.get(account_id, _DEFAULT_IBAN),
        "status": status,
        "currency": currency,
        # `type`, not `accountType` — payment_document.party_snapshot reads account["type"].
        # The fixture used the wrong key, so every snapshot silently got accountType: None.
        "type": account_type,
        "customerSnapshot": {"customerId": customer_id, "name": f"Holder {customer_id}"},
        "balance": {"current": available, "available": available, "ledger": available,
                    "updatedAt": datetime.now(timezone.utc)},
        # Enum values from the canonical spec: AccountSignatoryType /
        # AccountSignatorySigningRuleType / AccountRestrictionType.
        "signatories": signatories if signatories is not None else [
            {"customerId": customer_id, "type": "PRIMARY",
             "signingRule": signing_rule, "addedAt": "2024-12-07"},
        ],
        "restrictions": restrictions if restrictions is not None else [],
    }


# Stage 7 (doc 21 B1): the wire clearing account. A bank-internal `accounts` doc — `type:
# NOSTRO`, `gl.accountCode: 1131`, no `customerSnapshot` (the spec's `required` list omits
# `customerId`). `_money_move` `$inc`s its balance; `posting_rules._principal_gl_account`
# reads its `gl.accountCode`. Built here, not via `_account()`, because `_account()` requires
# a customer and sets `customerSnapshot` — the clearing account has neither.
_CLEARING_WIRE = {
    "accountId": "ACC-CLEARING-WIRE",
    "accountNumber": "1131-WIRE-CLEARING",
    "type": "NOSTRO",
    "status": "ACTIVE",
    "currency": "USD",
    "gl": {"accountCode": "1131", "costCenter": "CC-RETAIL-DEFAULT",
           "profitCenter": "PC-RETAIL-DEFAULT"},
    "balance": {"current": 0, "available": 0, "ledger": 0, "hold": 0, "overdraftLimit": 0,
                "updatedAt": datetime.now(timezone.utc)},
    "signatories": [],
    "restrictions": [],
}


def _customer(customer_id, *, status="ACTIVE", segment="RETAIL",
              customer_type="INDIVIDUAL", kyc_status="VERIFIED"):
    """Stage 2 reads `status`, `segment` and `kyc.status`; earlier stages read none of them.

    Enum values from the canonical spec: PartyApexStatus / CustomerSegmentType / PartyType /
    CustomerKYCProcedureStatus.
    """
    return {
        "customerId": customer_id,
        "name": f"Holder {customer_id}",
        # `party_snapshot` reads `identification.legalName`, and `_address_text` reads
        # `contact.addresses[]`. Both were absent, so every snapshot came out with
        # `name: None` / `address: None` — doc 17 §4's fixture-fidelity item, and the reason
        # stage 3's enrichment tests would otherwise assert against nulls.
        "identification": {"legalName": f"Holder {customer_id} Ltd."},
        "contact": {
            "addresses": [
                {"line1": "1 Test Street", "city": "New York", "state": "NY",
                 "postalCode": "10001", "country": "US"},
            ],
        },
        "status": status,
        "segment": segment,
        "type": customer_type,
        "kyc": {"status": kyc_status, "level": "STANDARD", "riskRating": "LOW"},
    }


DEBTOR, CREDITOR = "ACC-debtor01", "ACC-credit01"
CUST_D, CUST_C = "CUST-0000000001", "CUST-0000000002"


@pytest.fixture
def db():
    """Two open USD accounts with 10,000 each, owned by different customers."""
    return FakeDb({
        "accounts": FakeCollection([
            _account(DEBTOR, CUST_D), _account(CREDITOR, CUST_C),
            _CLEARING_WIRE,
        ]),
        "customers": FakeCollection([_customer(CUST_D), _customer(CUST_C)], key="customerId"),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
    })


@pytest.fixture
def service(db):
    db["payments"].unique_on = "idempotencyKey"  # mirrors idx_idempotency_key_unique
    return PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)


def _initiate(svc, **over):
    kwargs = dict(
        customer_ref=CUST_D,
        debtor_account_ref=DEBTOR,
        creditor_account_ref=CREDITOR,
        instructed_amount=250.0,
        instructed_currency="USD",
        payment_type="CREDIT_TRANSFER",
        # INTERNAL, not WIRE: both fixture accounts are Leafy Bank accounts, and since
        # stage 1 the rail<->envelope rules make that distinction real (a WIRE here would
        # be an internally-settled wire, which is not a thing).
        payment_rail="INTERNAL",
        remittance_unstructured="INV-48392",
    )
    kwargs.update(over)
    return svc.initiate_payment(**kwargs)


def _assert_rejected(db, *, reason_match, at_state=None):
    """A business rejection must leave a traceable payment, not nothing.

    Decision §11 (doc 11): the instruction is persisted at DRAFT before validation, so a
    rejected payment is a REJECTED document with an append-only event trail explaining where
    it died. This is what Doina's Stage 9 exception queue and the trace view read.
    """
    assert len(db["payments"].inserted) == 1, "the instruction must be recorded"
    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})

    assert payment["lifecycle"]["currentState"] == "REJECTED"
    assert payment["status"] == "REJECTED", "top-level status mirrors currentState"

    events = payment["lifecycle"]["events"]
    assert [e["state"] for e in events][0] == "DRAFT", "the trail starts at DRAFT"
    assert events[-1]["state"] == "REJECTED"
    assert reason_match in events[-1]["reason"], events[-1]["reason"]
    assert events[-1]["actor"], "every event names an actor"
    if at_state is not None:
        # the state the payment reached before being refused
        assert [e["state"] for e in events][-2] == at_state

    assert db["transactions"].inserted == [], "no money may move on a rejected payment"
    return payment


# --- 1. insufficient funds ----------------------------------------------------

def test_insufficient_funds_rejected_and_no_money_moves(service, db):
    """The stage 3 balance.available floor. No money moves; the refusal is traceable.

    This is Doina's own candidate Stage 9 demo failure ("invalid beneficiary or insufficient
    funds"), so the rejected payment must be inspectable — an exception queue needs a row.
    """
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(service, instructed_amount=10_000.01)

    _assert_rejected(db, reason_match="Insufficient available balance", at_state="INITIATED")
    for acc in db["accounts"].docs:
        if acc["type"] == "NOSTRO":
            continue  # clearing account starts at 0 — stage 7 B1
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
def test_currency_mismatch_warns_and_proceeds(db, debtor_ccy, creditor_ccy, instructed):
    """FR-3.14 — currency mismatch is a WARN, not a refusal. The payment
    proceeds; enrichment attaches a SIMULATED fxRate when the instructed
    currency differs from the debtor account currency."""
    db["accounts"] = FakeCollection([
        _account(DEBTOR, CUST_D, currency=debtor_ccy),
        _account(CREDITOR, CUST_C, currency=creditor_ccy),
    ])
    db["payments"].unique_on = "endToEndId"
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)

    # The currency_consistent check WARNs (stage 3 records outcomes, doc 17 R2/R12).
    payment = _initiate(svc, instructed_currency=instructed)

    # The payment was NOT rejected — it settled (internal transfer).
    assert payment["status"] == "SETTLED"

    warn = _one(db, "currency_consistent")
    assert warn["result"] == "WARN"
    assert warn["stage"] == "3 validate"
    assert "FX" in warn["detail"]

    # When the instructed currency differs from the debtor account currency,
    # enrichment attaches a SIMULATED fxRate and diverges `amount` (FR-3.14).
    # Otherwise no FX is applied (SKIP).
    if debtor_ccy != instructed:
        assert payment["fxRate"] is not None
        assert payment["amount"] != payment["instructedAmount"]
        assert payment["currency"] == debtor_ccy
    else:
        assert payment["fxRate"] is None


# --- 3. closed account --------------------------------------------------------

@pytest.mark.parametrize("side", ["debtor", "creditor"])
def test_closed_account_rejected(db, side):
    """A CLOSED account on either side refuses the payment, traceably."""
    accounts = [_account(DEBTOR, CUST_D), _account(CREDITOR, CUST_C)]
    accounts[0 if side == "debtor" else 1]["status"] = "CLOSED"
    db["accounts"] = FakeCollection(accounts)
    db["payments"].unique_on = "endToEndId"
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)

    with pytest.raises(ValueError, match="CLOSED"):
        _initiate(svc)

    _assert_rejected(db, reason_match="CLOSED", at_state="INITIATED")
    for acc in db["accounts"].docs:
        assert acc["balance"]["available"] == 10_000.0


# --- 4. duplicate idempotencyKey (idempotency) --------------------------------
#
# Stage 1 R7 split the two identifiers: `endToEndId` is the ISO 20022
# EndToEndIdentification and is always server-derived, while the caller's retry key now
# rides on `idempotencyKey`, where the unique sparse index lives.

def test_duplicate_idempotency_key_returns_first_payment_once(service, db):
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
              "idempotencyKey": "E2E-raced", "status": "SETTLED"}

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
    _assert_rejected(db, reason_match="must differ", at_state="INITIATED")


def test_debtor_account_not_owned_by_customer_rejected(service, db):
    """Stage 2 entitlement. Refused before validation, so the trail stops at INITIATED."""
    with pytest.raises(ValueError, match="not owned by"):
        _initiate(service, customer_ref=CUST_C)
    payment = _assert_rejected(db, reason_match="not owned by", at_state="INITIATED")
    assert "stage 2 authenticate" in payment["lifecycle"]["events"][-1]["reason"]


def test_amount_over_limit_rejected(service, db):
    """Basic-field validation is Doina's stage 1, so no instruction is ever created.

    Contrast with the account/funds rejections above, which DO leave a REJECTED document —
    those are stage 3 checks on an instruction that already exists (doc 11 §11).
    """
    with pytest.raises(ValueError, match="exceeds the limit"):
        _initiate(service, instructed_amount=50_000.01)
    assert db["payments"].inserted == []


@pytest.mark.parametrize("amount", [0.0, -1.0])
def test_non_positive_amount_rejected(service, db, amount):
    with pytest.raises(ValueError, match="greater than 0"):
        _initiate(service, instructed_amount=amount)
    assert db["payments"].inserted == []


# --- 9. the lifecycle state machine (doc 11 §11) -------------------------------

# Doina's happy path, with ROUTED inserted after FINAL_VALIDATED per decision D1.
# POSTED is absent by design: the ledger service stamps it asynchronously via CDC, on its
# own axis, and never through this saga.
EXPECTED_TRAIL = [
    "DRAFT",
    "INITIATED",
    "VALIDATED",
    "ENRICHED",
    "FINAL_VALIDATED",
    "ROUTED",
    "AUTHORISED",
    "APPROVED",
    "SUBMITTED",
    "IN_PROGRESS",
    "SETTLED",
]


def test_happy_path_walks_the_full_state_machine(service, db):
    """The nine stages must produce nine stages' worth of observable state.

    Before the state machine a payment went PENDING -> SETTLED: two data points for nine
    stages. This is the assertion that the stages are visible in the data, not just in the
    folder layout.
    """
    payment = _initiate(service, instructed_amount=250.0)

    assert [e["state"] for e in payment["lifecycle"]["events"]] == EXPECTED_TRAIL
    assert payment["lifecycle"]["currentState"] == "SETTLED"
    assert payment["status"] == "SETTLED", "top-level status mirrors currentState"
    assert payment["lifecycle"]["stateEnteredAt"] == payment["lifecycle"]["events"][-1]["at"]


def test_every_event_is_attributed_and_chronological(service, db):
    """Append-only, chronological, and every transition names who did it and why."""
    payment = _initiate(service, instructed_amount=250.0)
    events = payment["lifecycle"]["events"]

    for e in events:
        assert e["actor"], f"{e['state']} has no actor"
        assert e["actorType"] in ("SERVICE", "USER", "SYSTEM"), e["actorType"]
        assert e["reason"], f"{e['state']} has no reason"

    times = [e["at"] for e in events]
    assert times == sorted(times), "events must be chronological"

    # Attribution must be real, not a single hardcoded string.
    assert {e["actor"] for e in events} >= {"transactions-service", "fraud-service"}


def test_fraud_block_is_written_with_the_authorised_transition(service, db):
    """Stage 4b $sets the score at the same moment it advances — one write, not two.

    This used to assert `{"score": 5, "decision": "APPROVED"}` verbatim, which was the whole
    of stage 4b: a literal. It now asserts the SHAPE the spec requires — all five of
    `fraud`'s required sub-fields — and that the score is *derived* rather than constant,
    which is what R10/R22 actually asked for.
    """
    payment = _initiate(service, instructed_amount=250.0)
    fraud = payment["fraud"]

    assert set(fraud) == {"alertId", "score", "decision", "rulesFired", "checkedAt"}
    assert fraud["decision"] == "APPROVED"
    assert fraud["alertId"].startswith("FRAUD-")
    assert 0 <= fraud["score"] <= 100
    # A $250 domestic internal transfer trips no rule, so the score is the labelled model
    # baseline and nothing more — read from the module rather than duplicated here, so the
    # test cannot claim a number the code does not produce.
    assert fraud["score"] == fraud_rules.MODEL_BASELINE_SCORE
    assert fraud["rulesFired"] == []

    authorised = next(e for e in payment["lifecycle"]["events"] if e["state"] == "AUTHORISED")
    assert f"Fraud score {fraud['score']}" in authorised["reason"]
    assert "sanctions CLEAR" in authorised["reason"]
    assert authorised["actor"] == "fraud-service"


def test_settled_is_atomic_with_the_money_move(service, db):
    """SETTLED fires inside stage 5's ACID transaction, alongside clearing.settledAt."""
    payment = _initiate(service, instructed_amount=250.0)
    assert payment["clearing"]["settledAt"] is not None
    assert payment["lifecycle"]["currentState"] == "SETTLED"
    assert len(db["transactions"].inserted) == 1


def test_illegal_transition_is_refused():
    """The transition table is a guard, not documentation."""
    from contexts.payment_order_initiation.domain import lifecycle as L

    assert L.INITIATED in L.TRANSITIONS[L.DRAFT]
    assert L.SETTLED not in L.TRANSITIONS[L.DRAFT], "cannot settle straight from DRAFT"
    assert L.TRANSITIONS[L.REJECTED] == frozenset(), "terminals are terminal"

    # Money has moved by SUBMITTED, so an outright refusal is no longer available —
    # a correction must be a compensating outcome instead.
    assert L.REJECTED in L.TRANSITIONS[L.VALIDATED]
    assert L.REJECTED not in L.TRANSITIONS[L.SETTLED]
    assert L.REVERSED in L.TRANSITIONS[L.SETTLED]


def test_advance_rejects_a_state_it_cannot_reach(service, db):
    """A wrong from_state must fail loudly, so a lost race cannot double-transition."""
    from contexts.payment_order_initiation.domain import lifecycle as L

    payment = _initiate(service, instructed_amount=250.0)
    with pytest.raises(L.IllegalTransition):
        L.advance(
            db["payments"], payment["_id"], L.VALIDATED,
            actor="test", reason="going backwards", from_state=L.SETTLED,
        )


# --- 5. external beneficiary (doc 13 §2 B1) -----------------------------------
#
# Doina's flagship `wire_domestic` scenario: a creditor Leafy Bank does not hold. Stage 1
# captures it; stage 5 runs in full — mapping, submission, acknowledgement, both artifacts —
# and stops before the money move, because settling it needs the chart-of-accounts extension
# that has not been decided (doc 19 B1). These tests pin BOTH halves — that the payment is
# executed and traceable, and that not one cent moved.

EXTERNAL_CREDITOR = {
    "accountNo": "9876543210",
    "name": "Acme Corp",
    "bic": "CHASUS33",
    "bankName": "JPMorgan Chase",
    "bankCountry": "US",
}


def _initiate_external(svc, **over):
    return _initiate(
        svc,
        creditor_account_ref=None,
        creditor_party=EXTERNAL_CREDITOR,
        payment_rail="WIRE",
        **over,
    )


def test_external_wire_stays_at_in_progress_but_moves_money_to_clearing(service, db):
    """Stage 7 doc 21 B3 — the halt is gone, but settlement is still deferred.

    The rail has accepted the message, so the payment is at IN_PROGRESS (not SUBMITTED).
    The money has moved — debtor debited, clearing account credited, `transactions` doc
    written — but `SETTLED` has NOT fired. Settlement is deferred to `settle.py`, which
    owns the IN_PROGRESS → SETTLED transition once the simulated response arrives.

    The state is still inside `_POST_EXECUTION_TERMINALS`, so the payment can never become
    REJECTED from here, which is correct once a message has left the bank.
    """
    payment = _initiate_external(service)

    assert payment["lifecycle"]["currentState"] == "IN_PROGRESS"
    assert payment["status"] == "IN_PROGRESS"
    assert "settlement pending" in payment["lifecycle"]["events"][-1]["reason"]
    assert payment["clearing"]["settledAt"] is None, "SETTLED is deferred to settle.py"

    # Money moved: debtor debited, clearing account credited.
    assert len(db["transactions"].inserted) == 1, "the ledger now sees this payment"
    assert len(db["notifications"].inserted) == 1
    debtor = db["accounts"].find_one({"accountId": DEBTOR})
    assert debtor["balance"]["available"] == 9_750.0, "debtor was debited"
    clearing = db["accounts"].find_one({"accountId": "ACC-CLEARING-WIRE"})
    assert clearing["balance"]["available"] == 250.0, "clearing account holds the in-flight credit"


def test_external_wire_is_still_a_real_traceable_payment(service, db):
    """The halt is not a silent drop: the instruction exists, with its full event trail."""
    payment = _initiate_external(service)

    assert len(db["payments"].inserted) == 1
    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert states[0] == "DRAFT" and states[-1] == "IN_PROGRESS"
    assert states[-2] == "SUBMITTED", "both of stage 5's transitions fired, in order"
    assert "REJECTED" not in states, "the bank did not refuse this payment"
    assert payment["creditor"]["accountId"] is None
    assert payment["creditor"]["name"] == "Acme Corp"
    assert payment["clearing"]["submittedAt"] is not None
    assert payment["clearing"]["settledAt"] is None
    # The rail acknowledged it, and the acknowledgement is on the payment (R10, R11).
    assert payment["clearing"]["statusCode"] == "ACSP"
    assert payment["clearing"]["networkRef"], "the rail's own reference is recorded"


def test_internal_payment_still_settles_alongside_the_guard(service, db):
    """The guard must be scoped to external creditors only — the regression it could
    plausibly cause is halting everything."""
    payment = _initiate(service, instructed_amount=100.0)

    assert payment["status"] == "SETTLED"
    assert len(db["transactions"].inserted) == 1
    assert db["accounts"].find_one({"accountId": DEBTOR})["balance"]["available"] == 9_900.0
    assert db["accounts"].find_one({"accountId": CREDITOR})["balance"]["available"] == 10_100.0


def test_missing_creditor_rolls_the_money_move_back(service, db, monkeypatch):
    """The defensive assertion inside the ACID block. The external guard makes this
    unreachable in practice; this test proves the block aborts rather than destroying money
    if it ever becomes reachable again.

    The creditor has to vanish BETWEEN capture (which resolves it) and the money move —
    that is the only way this branch is reached — so the credit update is stubbed to
    no-match rather than the account being deleted up front.
    """
    real = db["accounts"].find_one_and_update

    def credit_matches_nothing(flt, update, *a, **kw):
        if flt.get("accountId") == CREDITOR:
            return None
        return real(flt, update, *a, **kw)

    monkeypatch.setattr(db["accounts"], "find_one_and_update", credit_matches_nothing)

    with pytest.raises(ValueError, match="did not match at settlement time"):
        _initiate(service)

    assert db["transactions"].inserted == [], "no transaction on a failed money move"


# --- 12. stage 2 — party authentication & entitlement (doc 15) -----------------
#
# Stage 2 is a GATE: it records six `checks[]` entries and moves the payment nowhere
# (doc 15 B5 — Doina's sequence goes INITIATED -> VALIDATED with nothing between). So the
# assertions below are about the check trail and the refusals, never about a new state.
#
# One test per doc-15 requirement row that changes runtime behaviour: R1, R4, R5, R6, R7,
# R8/R9. R3 (the tick display) is doc 16's; R11 (sync/async) is asserted on every entry.

STAGE_2 = "2 authenticate"


def _checks(db, *, name=None, stage=None):
    """`payments.checks[]`, optionally filtered.

    `stage` matters from stage 3 on: every stage appends to ONE array (doc 15 B3) and a
    consumer filters by `stage`, so a test that asserts an exact name list must say which
    stage it means or it breaks every time a later stage lands.
    """
    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    entries = payment.get("checks", [])
    return [
        c for c in entries
        if (name is None or c["name"] == name) and (stage is None or c["stage"] == stage)
    ]


def _one(db, name):
    found = _checks(db, name=name)
    assert len(found) == 1, f"expected exactly one {name} check, got {len(found)}"
    return found[0]


def _service_for(db, **over):
    db["payments"].unique_on = "idempotencyKey"
    return PaymentsService(FakeConnection(db), "leafy_bank_bian",
                           payment_limit_usd=over.get("payment_limit_usd", 50_000.0))


_ASSERTION = {"method": "OTP", "factorCount": 2, "sessionRef": "SESS-7781",
              "authenticatedAt": datetime.now(timezone.utc)}


# --- the trail ----------------------------------------------------------------

def test_stage_two_records_its_six_checks_in_order(service, db):
    _initiate(service)
    recorded = _checks(db, stage="2 authenticate")
    assert [c["name"] for c in recorded] == [
        "customer_authenticated", "account_active", "account_unrestricted",
        "customer_entitled", "payment_limit_available", "dual_approval",
    ]
    assert all(c["stage"] == STAGE_2 for c in recorded)
    assert all(c["mode"] == "SYNC" for c in recorded), "R11 — every entry declares its mode"
    assert all(c["actor"] and c["at"] and c["detail"] for c in recorded)


def test_stage_two_adds_no_lifecycle_state(service, db):
    """B5. Its visibility comes from `checks[]`, not from the state machine."""
    _initiate(service)
    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert states[states.index("INITIATED") + 1] == "VALIDATED", "nothing in between"


def test_the_failing_check_is_recorded_before_the_rejection(service, db):
    """A refusal's whole value to the demo is knowing WHICH check refused it, so the trail
    has to be flushed before the ValueError the saga turns into REJECTED."""
    with pytest.raises(ValueError, match="not owned by"):
        _initiate(service, customer_ref=CUST_C)
    failed = [c for c in _checks(db) if c["result"] == "FAIL"]
    assert [c["name"] for c in failed] == ["customer_entitled"]
    assert "not owned by" in failed[0]["detail"]


# --- R1: the channel's authentication assertion -------------------------------

def test_no_assertion_is_recorded_as_skip_never_pass(service, db):
    """B1 — the demo must never claim an authentication that did not happen."""
    _initiate(service)
    check = _one(db, "customer_authenticated")
    assert check["result"] == "SKIP"
    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    assert payment["authentication"]["method"] == "NONE"
    assert payment["authentication"]["sufficient"] is False


def test_an_asserted_authentication_passes_and_is_recorded(service, db):
    _initiate(service, authentication=_ASSERTION)
    assert _one(db, "customer_authenticated")["result"] == "PASS"
    assessment = db["payments"].find_one(
        {"paymentId": db["payments"].inserted[0]["paymentId"]})["authentication"]
    assert assessment["method"] == "OTP"
    assert assessment["factorCount"] == 2
    assert assessment["sessionRef"] == "SESS-7781"
    assert assessment["assessedBy"] == "transactions-service"


def test_a_weak_factor_holds_above_the_step_up_threshold(service, db):
    """RETAIL steps up above 2,500; one password does not carry 5,000.

    Since 2026-09-09 this is a HOLD, not a rejection: the payment stays ONE document at
    INITIATED with `stepUpRequired` set, and the channel resumes it with a second factor —
    no REJECTED row, no second document (Kiran).
    """
    weak = {"method": "PASSWORD", "factorCount": 1}
    held = _initiate(service, instructed_amount=5_000.0, authentication=weak)
    assert held["stepUpRequired"] is True
    assert held["status"] == "INITIATED"
    assert db["transactions"].inserted == [], "no money moved while held"
    assert len(db["payments"].inserted) == 1


def test_resuming_a_held_payment_completes_the_same_document(service, db):
    """Resume continues the SAME payment from stage 2 — one id end to end, never a second doc,
    never a REJECTED row."""
    weak = {"method": "PASSWORD", "factorCount": 1}
    held = _initiate(service, instructed_amount=5_000.0, authentication=weak)
    payment_id = held["paymentId"]

    done = service.resume_payment(payment_id, customer_ref=CUST_D, authentication=_ASSERTION)

    assert done["paymentId"] == payment_id, "the resumed payment is the SAME document"
    assert done["status"] == "SETTLED"
    assert len(db["payments"].inserted) == 1, "must not create a second document"
    # The one document ends SETTLED, not REJECTED, and never showed a REJECTED blip.
    assert db["payments"].find_one({"paymentId": payment_id})["status"] == "SETTLED"
    assert db["transactions"].inserted, "the resumed payment moved money exactly once"
    assert len(db["transactions"].inserted) == 1


# --- R4: account_active -------------------------------------------------------

@pytest.mark.parametrize("status", ["DORMANT", "FROZEN", "CLOSED"])
def test_an_unusable_debtor_account_is_refused_at_stage_two(db, status):
    """DORMANT and FROZEN are canonical `CurrentAccountApexStatus` values that no code
    checked before — a frozen account could be debited. CLOSED moved here from stage 3."""
    db["accounts"] = FakeCollection([_account(DEBTOR, CUST_D, status=status),
                                     _account(CREDITOR, CUST_C)])
    svc = _service_for(db)
    with pytest.raises(ValueError, match=status):
        _initiate(svc)
    assert _one(db, "account_active")["result"] == "FAIL"
    _assert_rejected(db, reason_match=f"stage {STAGE_2}", at_state="INITIATED")
    for acc in db["accounts"].docs:
        assert acc["balance"]["available"] == 10_000.0


# --- R6: account_unrestricted (BIAN CustomerAccessEntitlement/Restrictions/Evaluate) ---

@pytest.mark.parametrize("kind", ["DEBIT_BLOCK", "FULL_BLOCK", "LEGAL_HOLD"])
def test_a_restricted_debtor_account_is_refused(db, kind):
    restriction = [{"type": kind, "reason": "Under investigation",
                    "appliedAt": "2026-08-20", "appliedBy": "OPS", "expiresAt": None}]
    db["accounts"] = FakeCollection([_account(DEBTOR, CUST_D, restrictions=restriction),
                                     _account(CREDITOR, CUST_C)])
    svc = _service_for(db)
    with pytest.raises(ValueError, match=kind):
        _initiate(svc)
    assert _one(db, "account_unrestricted")["result"] == "FAIL"
    assert db["transactions"].inserted == []


def test_a_lapsed_restriction_does_not_refuse(db):
    lapsed = [{"type": "DEBIT_BLOCK", "reason": "Resolved", "appliedAt": "2026-01-01",
               "appliedBy": "OPS", "expiresAt": "2026-02-01"}]
    db["accounts"] = FakeCollection([_account(DEBTOR, CUST_D, restrictions=lapsed),
                                     _account(CREDITOR, CUST_C)])
    svc = _service_for(db)
    _initiate(svc)
    assert _one(db, "account_unrestricted")["result"] == "PASS"


# --- R5: customer_entitled ----------------------------------------------------

def test_a_party_who_is_not_a_signatory_cannot_debit_the_account(db):
    """`signatories[]` was never read by any live code before this stage."""
    someone_else = [{"customerId": "CUST-0000000099", "type": "PRIMARY",
                     "signingRule": "SOLE", "addedAt": "2024-12-07"}]
    db["accounts"] = FakeCollection([_account(DEBTOR, CUST_D, signatories=someone_else),
                                     _account(CREDITOR, CUST_C)])
    svc = _service_for(db)
    with pytest.raises(ValueError, match="not a signatory"):
        _initiate(svc)
    assert _one(db, "customer_entitled")["result"] == "FAIL"


@pytest.mark.parametrize("over,match", [
    ({"status": "SUSPENDED"}, "SUSPENDED"),
    ({"kyc_status": "PENDING"}, "KYC status is PENDING"),
])
def test_an_ineligible_customer_cannot_debit_the_account(db, over, match):
    db["customers"] = FakeCollection([_customer(CUST_D, **over), _customer(CUST_C)],
                                     key="customerId")
    svc = _service_for(db)
    with pytest.raises(ValueError, match=match):
        _initiate(svc)
    assert _one(db, "customer_entitled")["result"] == "FAIL"


# --- R7: payment_limit_available ----------------------------------------------

def test_an_amount_within_the_global_bound_can_still_exceed_the_entitlement(db):
    """The point of B2. The global `PAYMENT_LIMIT_USD` is a malformed-input bound; the
    entitlement decision is per segment, and this payment passes the first and fails the
    second — which one global scalar could never express."""
    svc = _service_for(db)                      # global bound 50,000
    with pytest.raises(ValueError, match="exceeds the RETAIL per-payment entitlement"):
        _initiate(svc, instructed_amount=30_000.0)   # RETAIL entitlement 25,000
    assert _one(db, "payment_limit_available")["result"] == "FAIL"
    assert db["transactions"].inserted == []


# --- R8/R9: dual approval -----------------------------------------------------

def test_a_small_retail_payment_needs_no_second_approver(service, db):
    _initiate(service)
    check = _one(db, "dual_approval")
    assert check["result"] == "SKIP"
    assert "no second approver required" in check["detail"]


def _corporate_db():
    """Doina's scenario: a COMMERCIAL customer on a JOINT-mandate account (doc 15 B7).

    Mirrors the seed fixture added in `backend/data/sample`: the initiator is the account
    holder — stage 2's ownership assertion requires that — and the second signatory exists
    so `signingRule: JOINT` describes a real two-signer mandate.
    """
    signatories = [
        {"customerId": CUST_D, "type": "PRIMARY", "signingRule": "JOINT", "addedAt": "2023-03-14"},
        {"customerId": "CUST-abc10002", "type": "JOINT", "signingRule": "JOINT", "addedAt": "2023-03-14"},
    ]
    return FakeDb({
        "accounts": FakeCollection([
            _account(DEBTOR, CUST_D, available=250_000.0, signatories=signatories),
            _account(CREDITOR, CUST_C),
        ]),
        "customers": FakeCollection(
            [_customer(CUST_D, segment="COMMERCIAL", customer_type="CORPORATE"),
             _customer(CUST_C)], key="customerId"),
        "payments": FakeCollection(key="paymentId"),
        "transactions": FakeCollection(key="transactionId"),
        "notifications": FakeCollection(key="notificationId"),
    })


def test_the_flagship_corporate_payment_settles_with_a_labelled_simulated_approver():
    """R9 end to end: $25,000 from a COMMERCIAL customer trips her own $10,000 threshold,
    shows dual approval, and still settles — B4's whole point. The simulation is labelled,
    because an unlabelled tick on a banker's screen would be a lie.
    """
    db = _corporate_db()
    svc = _service_for(db, payment_limit_usd=1_000_000.0)
    _initiate(svc, instructed_amount=25_000.0, authentication=_ASSERTION)

    check = _one(db, "dual_approval")
    assert check["result"] == "PASS"
    assert check["actor"] == "SIMULATED-APPROVER-OPS"
    assert "SIMULATED" in check["detail"]

    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    assert payment["entitlement"]["dualApprovalRequired"] is True
    assert payment["entitlement"]["segment"] == "COMMERCIAL"
    assert payment["entitlement"]["signingRule"] == "JOINT"
    assert payment["lifecycle"]["currentState"] == "SETTLED", "the demo still runs end to end"
    assert len(db["transactions"].inserted) == 1


def test_stage_four_reports_the_stage_two_decision_instead_of_a_hardcoded_string():
    """`evaluate.py` used to assert "No dual-approval threshold breached" with no check
    behind it. The APPROVED event now quotes stage 2 (doc 15 B4)."""
    db = _corporate_db()
    svc = _service_for(db, payment_limit_usd=1_000_000.0)
    _initiate(svc, instructed_amount=25_000.0)

    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    approved = [e for e in payment["lifecycle"]["events"] if e["state"] == "APPROVED"]
    assert len(approved) == 1
    assert "SIMULATED-APPROVER-OPS" in approved[0]["reason"]


def test_a_sole_mandate_below_the_threshold_reports_it_as_such(service, db):
    _initiate(service)
    payment = db["payments"].find_one({"paymentId": db["payments"].inserted[0]["paymentId"]})
    approved = [e for e in payment["lifecycle"]["events"] if e["state"] == "APPROVED"]
    assert "Below the RETAIL dual-approval threshold" in approved[0]["reason"]


# --- 13. the token-derived assertion (Level 1 authentication) -----------------
#
# `main.py` resolves identity from the `Authorization` header and overrides the body's
# `customer_ref` / `authentication` with it. These tests take the resolver's OUTPUT and
# feed it to the service the way the route does, so the two halves are pinned together —
# a change to the resolver's shape fails here rather than at runtime.

def test_a_token_derived_assertion_reaches_the_payment(service, db):
    """The assertion stage 2 grades now comes from a signature, not from the request."""
    from datetime import timedelta

    import jwt

    from shared import party_authentication_token as party_auth

    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "sub": CUST_D, "callerType": "CUSTOMER", "method": "OTP", "factorCount": 2,
        "sessionRef": "SESS-REAL01", "authenticatedAt": now.isoformat(),
        "iss": party_auth.ISSUER, "aud": party_auth.AUDIENCE,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(hours=1)).timestamp()),
    }, party_auth._DEV_SECRET, algorithm=party_auth.ALGORITHM)

    identity = party_auth.resolve_identity(f"Bearer {token}", CUST_D)
    _initiate(service, **identity)

    assert _one(db, "customer_authenticated")["result"] == "PASS"
    assessment = db["payments"].find_one(
        {"paymentId": db["payments"].inserted[0]["paymentId"]})["authentication"]
    assert assessment["method"] == "OTP"
    assert assessment["factorCount"] == 2
    assert assessment["sessionRef"] == "SESS-REAL01"
    assert assessment["callerType"] == "CUSTOMER", "who held the token, not just what they used"


def test_without_a_token_the_stage_still_records_a_skip(service, db):
    """`REQUIRE_AUTHENTICATION` off: the payment goes through and the check reads SKIP.
    The rollout must not silently upgrade an absent assertion to a pass."""
    from shared import party_authentication_token as party_auth

    _initiate(service, **party_auth.resolve_identity(None, CUST_D))
    assert _one(db, "customer_authenticated")["result"] == "SKIP"
