"""Stage 6 — accounting / posting. Doc 20's gates, one test per R-row that changes runtime.

Covers the payments write-back (B1) and the fee legs (B3). The pre-existing ledger suite
tests pure functions only, so `write_journal` had no coverage at all before this file — which
is why the write-back tests drive it through a fake connection rather than asserting on a
builder's return value.

⚠️ `glBalanceHistory` / `PositionKeeping` (doc 20 B2, R6/R7) were **dropped on Kiran's call,
2026-09-02** — not deferred by the plan, and no balance-snapshot collection is created.
"""

from __future__ import annotations

import pathlib

import pytest

from services.journal_service import build_journal_entry, write_journal
from tests._fakedb import FakeConnection

_PERIOD = "2026-06"
_BATCH = "BATCH-20260623T1415"


def _agg(code: str, side: str, amount: int) -> dict:
    return {
        "_id": {"controlAccountCode": code, "side": side},
        "amount": amount,
        "currency": "USD",
        "count": 1,
        "subLedgerIds": [f"SL-{code}-{side}-001"],
        "eventIds": [f"LE-{code}-{side}-001"],
    }


def _journal(amount: int = 25000):
    return build_journal_entry(
        _BATCH, _PERIOD,
        [_agg("2110", "DEBIT", amount), _agg("2120", "CREDIT", amount)],
    )


# --- write-back to payments (step 4, B1 / R4) --------------------------------

_GL_2110 = {
    "accountCode": "2110", "accountName": "Current Accounts - Control",
    "accountType": "LIABILITY", "isPostingAccount": False, "status": "ACTIVE",
}

_PAY = "PAY-100023"
_EVENT = "LE-0001"


def _with_payment(state: str = "IN_PROGRESS", **extra) -> FakeConnection:
    c = FakeConnection()
    c.seed("glAccounts", [_GL_2110])
    c.seed("ledgerEvents", [{"eventId": _EVENT, "idempotencyKey": _PAY}])
    # ⚠️ `txnId` — the production field name (`payment_rail/documents.py:45`). This fixture
    # said `transactionId` (the spec's name for the FK) until 2026-09-02, so the write-back
    # read a key that never exists and `refs.transactionId` stayed null on the live cluster
    # while this test passed. Phase C's "check fixtures for fidelity" rule, and the same
    # class as stage 1's `_account()` setting `accountType` where the code reads `type`.
    c.seed("transactions", [{"paymentId": _PAY, "txnId": "TXN-0001"}])
    c.seed("payments", [{
        "paymentId": _PAY,
        "status": state,
        "lifecycle": {"currentState": state, "events": [{"state": state}]},
        "refs": {"journalEntryId": None, "ledgerEventId": None, "transactionId": None},
        **extra,
    }])
    return c


def _post(c: FakeConnection):
    journal, sub_ids, event_ids = build_journal_entry(
        _BATCH, _PERIOD,
        [_agg("2110", "DEBIT", 25000), _agg("2120", "CREDIT", 25000)],
    )
    # build_journal_entry derives eventIds from the agg rows; point them at our seeded event.
    write_journal(journal, sub_ids, [_EVENT], c, "db")
    return journal


def test_an_in_progress_payment_reaches_posted_with_exactly_one_new_event():
    c = _with_payment("IN_PROGRESS")
    _post(c)
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "POSTED"
    assert p["status"] == "POSTED"
    states = [e["state"] for e in p["lifecycle"]["events"]]
    assert states == ["IN_PROGRESS", "POSTED"]


def test_a_settled_payment_keeps_its_state_but_gains_the_posting_fact():
    """POSTED genuinely arrives after SETTLED for an internal transfer — it settles
    synchronously and the batch posts ten minutes later (lifecycle.py:82-84)."""
    c = _with_payment("SETTLED")
    journal = _post(c)
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "SETTLED"
    assert p["lifecycle"]["postingStatus"] == "POSTED"
    assert p["refs"]["journalEntryId"] == journal["journalId"]
    assert [e["state"] for e in p["lifecycle"]["events"]] == ["SETTLED"]


def test_all_four_spec_fields_are_written():
    """The four that were 0-hit greps before stage 6 (doc 20 §1b)."""
    c = _with_payment("IN_PROGRESS")
    journal = _post(c)
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["postingStatus"] == "POSTED"
    assert p["refs"]["journalEntryId"] == journal["journalId"]
    assert p["refs"]["ledgerEventId"] == _EVENT
    assert p["refs"]["transactionId"] == "TXN-0001"


def test_a_replayed_batch_appends_no_second_lifecycle_event():
    c = _with_payment("IN_PROGRESS")
    _post(c)
    from services.posting_writeback_service import write_back
    write_back("JNL-REPLAY", [_EVENT], c, "db")     # same payment, already POSTED
    p = c.get_collection("db", "payments").docs[0]
    assert [e["state"] for e in p["lifecycle"]["events"]] == ["IN_PROGRESS", "POSTED"]


def test_a_write_back_failure_leaves_the_journal_committed():
    """The write-back is OUTSIDE the journal's transaction (B1(a)): failing to stamp an
    operational record must never roll back a balanced, committed journal."""
    c = _with_payment("IN_PROGRESS")
    c.raise_on = "payments"
    journal = _post(c)
    assert c.rolled_back is False
    assert len(c.get_collection("db", "journalEntries").docs) == 1
    # And the payment is untouched rather than half-written.
    assert c.get_collection("db", "payments").docs[0]["lifecycle"]["currentState"] == "IN_PROGRESS"


def test_the_written_event_matches_the_state_machines_own_shape():
    """The ledger writes `lifecycle.events[]` without importing the transactions service's
    state machine (B1(d)). This is the assertion that keeps the two shapes in agreement."""
    c = _with_payment("IN_PROGRESS")
    _post(c)
    event = c.get_collection("db", "payments").docs[0]["lifecycle"]["events"][-1]
    assert set(event) == {"state", "at", "actor", "actorType", "reason"}


def test_payment_ids_come_from_ledger_events_not_from_payments():
    from services.posting_writeback_service import payment_ids_for_events
    c = _with_payment()
    assert payment_ids_for_events([_EVENT], c, "db") == {_EVENT: _PAY}


def test_the_derivation_path_never_reads_payments():
    """The half of the standing rule that still holds (B1). `pipeline_read_service` and
    `reconciliation_service.compute_reconciliation` (stage 8) read `payments` for read-only
    composition / post-batch tie-out and always may; the DERIVATION path must not.

    `reconciliation_service.py` was in this list when its only job was the pre-batch gate
    (subledger↔journal, no `payments`). Stage 8 added `compute_reconciliation`, which reads
    `payments` post-batch — so the file is no longer purely derivation and is removed from the
    grep. The positive companion test below asserts the payments-reading function does not reach
    the pure derivation workers."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for rel in ("workers/ingest_worker.py", "workers/projection_worker.py",
                "workers/gl_batch.py", "shared/posting_rules.py",
                "services/subledger_service.py", "services/journal_service.py"):
        text = (root / rel).read_text()
        if '"payments"' in text:
            offenders.append(rel)
    assert offenders == [], f"derivation path reads payments: {offenders}"


def test_the_reconciliation_pass_reads_payments_but_does_not_derive():
    """Stage 8 companion to the firewall guard (doc 22 §4). `compute_reconciliation` reads
    `payments` — that is permitted because it is a read-only post-batch check, not accounting
    derivation. Assert it is not imported by the pure derivation workers, so the boundary the
    grep test draws (derivation files contain no `"payments"`) stays meaningful even though
    `gl_batch` calls the post-batch pass."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    derivation = ("workers/ingest_worker.py", "workers/projection_worker.py",
                  "shared/posting_rules.py", "services/subledger_service.py",
                  "services/journal_service.py")
    offenders = []
    for rel in derivation:
        text = (root / rel).read_text()
        if "compute_reconciliation" in text or "reconcile_settled_payments" in text:
            offenders.append(rel)
    assert offenders == [], f"derivation worker imports the reconciliation pass: {offenders}"


# --- the fee posting rule (step 6, B3) ---------------------------------------

from shared.coa_cache import ChartOfAccounts                       # noqa: E402
from shared.posting_rules import (                                  # noqa: E402
    EVENT_PAYMENT_FEE,
    SIDE_CREDIT,
    SIDE_DEBIT,
    decompose_fee,
    fee_gl_account,
)
from workers.ingest_worker import build_fee_event, build_ledger_event  # noqa: E402

_FEE_LEAF = {"accountCode": "4211", "accountName": "Transaction Fee Income",
             "accountType": "REVENUE", "isPostingAccount": True, "status": "ACTIVE",
             "parentAccountCode": "4210"}
_FEE_ROLLUP = {"accountCode": "4200", "accountName": "Fee Income", "accountType": "REVENUE",
               "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "4000"}


def _coa(*extra: dict) -> ChartOfAccounts:
    return ChartOfAccounts([
        {"accountCode": "2100", "accountName": "Customer Deposits", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "2110", "accountName": "Current Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2111", "accountName": "Personal Current Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2110"},
        {"accountCode": "2120", "accountName": "Savings Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2121", "accountName": "Personal Savings Accounts", "isPostingAccount": True, "status": "ACTIVE", "parentAccountCode": "2120"},
        {"accountCode": "4000", "accountName": "Income", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": None},
        {"accountCode": "4210", "accountName": "Fee Income - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "4000"},
        *extra,
    ])


def _acct(account_id: str, gl_code: str) -> dict:
    return {"accountId": account_id, "gl": {"accountCode": gl_code}}


def _wire_txn(**over) -> dict:
    base = {
        "paymentId": "PAY-abc12345",
        "amount": 250.00, "currency": "USD",
        "rail": "WIRE", "paymentType": "CREDIT_TRANSFER",
        "payer": {"accountId": "ACC-debtor"}, "payee": {"accountId": "ACC-creditor"},
        "feeAmount": 25.00, "feeCurrency": "USD",
    }
    base.update(over)
    return base


def test_the_fee_leaf_is_4211_not_the_4200_rollup():
    """⚠️ The reserved hook said `4200` until stage 6. `4200 Fee Income` is
    `isPostingAccount: false` — a rollup. A leg naming it crashes `projection_worker` on a
    change-stream event (defect 2026-07-01, fired twice)."""
    assert fee_gl_account(_coa(_FEE_LEAF)) == "4211"


def test_a_non_leaf_fee_account_resolves_to_none_rather_than_raising():
    """The exact trap `4200` fell into: present, ACTIVE, and not postable."""
    assert fee_gl_account(_coa(_FEE_ROLLUP)) is None


def test_an_absent_fee_account_resolves_to_none_rather_than_raising():
    assert fee_gl_account(_coa()) is None


def test_a_wire_with_a_fee_produces_four_legs_in_total():
    """Two events, two legs each — `ledgerEvents` carries one debit/credit pair per document
    (decisions.md 2026-06-18), so four legs cannot be one event."""
    coa = _coa(_FEE_LEAF)
    principal = build_ledger_event(_wire_txn(), _acct("ACC-debtor", "2111"),
                                   _acct("ACC-creditor", "2121"), coa)
    fee = build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa)
    assert fee is not None
    legs = [principal["debitLeg"], principal["creditLeg"],
            fee["debitLeg"], fee["creditLeg"]]
    assert len(legs) == 4
    debits = principal["debitLeg"]["amount"] + fee["debitLeg"]["amount"]
    credits = principal["creditLeg"]["amount"] + fee["creditLeg"]["amount"]
    assert debits == credits == 27500          # 250.00 + 25.00, in minor units


def test_the_fee_credits_fee_income_and_debits_the_customer():
    coa = _coa(_FEE_LEAF)
    fee = build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa)
    assert fee["debitLeg"]["glAccountCode"] == "2111"
    assert fee["creditLeg"]["glAccountCode"] == "4211"
    assert fee["eventType"] == EVENT_PAYMENT_FEE


def test_the_fee_event_does_not_collide_with_the_principals_idempotency_key():
    """`trace_payment` looks the principal up by `idempotencyKey == paymentId`, so that value
    must not change and the fee must not take it."""
    coa = _coa(_FEE_LEAF)
    principal = build_ledger_event(_wire_txn(), _acct("ACC-debtor", "2111"),
                                   _acct("ACC-creditor", "2121"), coa)
    fee = build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa)
    assert principal["idempotencyKey"] == "PAY-abc12345"
    assert fee["idempotencyKey"] == "PAY-abc12345-FEE"


def test_an_internal_transfer_produces_no_fee_event():
    coa = _coa(_FEE_LEAF)
    assert build_fee_event(_wire_txn(feeAmount=0.0), _acct("ACC-debtor", "2111"), coa) is None
    txn = _wire_txn()
    del txn["feeAmount"]
    assert build_fee_event(txn, _acct("ACC-debtor", "2111"), coa) is None


def test_a_fee_with_no_income_leaf_is_skipped_and_does_not_raise():
    """⚠️ The crash-loop guard. With no `4211`, the principal must still post."""
    coa = _coa()                                   # no fee account at all
    assert build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa) is None
    principal = build_ledger_event(_wire_txn(), _acct("ACC-debtor", "2111"),
                                   _acct("ACC-creditor", "2121"), coa)
    assert principal["debitLeg"]["amount"] == 25000


def test_a_fee_with_a_non_leaf_income_account_is_skipped_and_does_not_raise():
    coa = _coa(_FEE_ROLLUP)
    assert build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa) is None


def test_decompose_fee_returns_a_balanced_pair():
    legs = decompose_fee(fee_amount=25.00, currency="USD",
                         debtor_account=_acct("ACC-debtor", "2111"), coa=_coa(_FEE_LEAF))
    assert len(legs) == 2
    assert sum(l.amount_minor for l in legs if l.side == SIDE_DEBIT) == \
           sum(l.amount_minor for l in legs if l.side == SIDE_CREDIT)


def test_the_mapping_version_was_bumped_for_the_fee_rule():
    """Stamped on every emitted event, so a rule change must move it (doc 20 step 6).

    Stage 7 bumped it again (1.1.0 → 1.2.0) for the settlement rule (doc 21 B2).
    """
    from shared.posting_rules import MAPPING_VERSION
    assert MAPPING_VERSION == "1.2.0"


# --- enum parity for the values the LEDGER writes onto payments ---------------

# tests/ -> ledger/ -> backend/ -> leaf-bank-bian/ -> payment-flow-full/
_SPEC = (pathlib.Path(__file__).resolve().parents[4]
         / "doinas-research" / "propose_payments.json")


def _payments_schema():
    import json
    spec = json.loads(_SPEC.read_text())
    return spec["collections"]["payments"]["validator"]["$and"][0]["$jsonSchema"]["properties"]


def test_the_write_backs_values_are_all_in_their_spec_enums():
    """Stage 6 is the first stage where the LEDGER writes `payments`, so the enum-drift
    prevention rule (defect 2026-04-28) has to reach across the service boundary.

    `transactions/tests/test_payment_document_spec.py` walks the freshly-built document and
    cannot see these: `postingStatus` is absent at creation, and the `POSTED` lifecycle event
    is appended by this service ten minutes later. Sourced from the spec's own `enum`
    arrays — never hand-rolled.
    """
    from services import posting_writeback_service as wb
    props = _payments_schema()

    assert wb._POSTED in props["lifecycle"]["properties"]["postingStatus"]["enum"]
    assert wb._POSTED in props["lifecycle"]["properties"]["currentState"]["enum"]
    assert wb._POSTED in props["status"]["enum"]
    assert wb._IN_PROGRESS in props["lifecycle"]["properties"]["currentState"]["enum"]

    event_props = props["lifecycle"]["properties"]["events"]["items"]["properties"]
    assert wb._POSTED in event_props["state"]["enum"]
    assert wb._ACTOR_TYPE in event_props["actorType"]["enum"]


def test_every_ref_the_write_back_sets_is_a_spec_declared_field():
    """The four fields doc 20 R4 exists to populate. All were 0-hit greps before stage 6."""
    props = _payments_schema()
    refs = props["refs"]["properties"]
    for field in ("journalEntryId", "ledgerEventId", "transactionId"):
        assert field in refs, f"refs.{field} is not in the spec"
    assert "postingStatus" in props["lifecycle"]["properties"]


def test_the_fee_event_does_not_file_its_revenue_under_customer_deposits():
    """`subledger_service:33` copies `meta.subLedgerType` onto every subLedgerEntries row, so
    a hardcoded CUSTOMER_DEPOSITS would mislabel the fee's revenue credit — silently, and
    visibly only to someone reading the rows."""
    coa = _coa(_FEE_LEAF)
    principal = build_ledger_event(_wire_txn(), _acct("ACC-debtor", "2111"),
                                   _acct("ACC-creditor", "2121"), coa)
    fee = build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), coa)
    assert principal["meta"]["subLedgerType"] == "CUSTOMER_DEPOSITS"
    assert fee["meta"]["subLedgerType"] == "FEE_INCOME"


def test_the_fee_credit_rolls_up_to_the_fee_income_control_account():
    """`4211` -> `4210 Fee Income - Control`. If the CoA lacked that control ancestor,
    `control_account_for` would raise inside the worker."""
    fee = build_fee_event(_wire_txn(), _acct("ACC-debtor", "2111"), _coa(_FEE_LEAF))
    assert fee["creditLeg"]["controlAccountCode"] == "4210"


def test_a_broken_chart_of_accounts_cannot_crash_loop_the_worker():
    """⚠️ 2026-07-01 prevention rule. The principal is inserted BEFORE the fee is built, so a
    raise escaping `process_transaction` would leave the resume token unsaved and replay the
    same change-stream event forever. A missing fee leg degrades the demo; a crash-looping
    worker stops it."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "workers" / "ingest_worker.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "process_transaction")
    guarded = [
        h for t in ast.walk(fn) if isinstance(t, ast.Try) for h in t.handlers
        if any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "build_fee_event"
               for c in ast.walk(t))
    ]
    assert guarded, "build_fee_event is not inside a try/except in process_transaction"


def test_the_transaction_ref_is_read_from_the_field_the_collection_actually_uses():
    """⚠️ Regression for the 2026-09-02 live bug: the write-back read `transactionId` (the
    spec's name for the FK) while `transactions` stores `txnId`, so `refs.transactionId`
    stayed permanently null while the other three refs wrote fine.

    Asserts the *service* reads `txnId` — a fixture is what hid the bug, so this test
    deliberately does not rely on one.
    """
    import inspect

    from services import posting_writeback_service as wb
    src = inspect.getsource(wb._write_back)
    assert '"txnId"' in src, "the write-back must project txnId, the production field name"


def test_a_transaction_without_a_txn_id_leaves_the_ref_unset_rather_than_guessing():
    c = _with_payment("SETTLED")
    c.get_collection("db", "transactions").docs[0].pop("txnId")
    _post(c)
    p = c.get_collection("db", "payments").docs[0]
    assert p["refs"]["transactionId"] is None
    # The other three still land — one missing ref must not block the posting fact.
    assert p["lifecycle"]["postingStatus"] == "POSTED"
