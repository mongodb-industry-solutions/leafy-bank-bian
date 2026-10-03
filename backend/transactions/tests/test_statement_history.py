"""Plan B2 — backfilled statement history: the evidence the reconciliation agent reasons over."""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from datetime import datetime, timezone

from contexts.financial_gateway.application.statement import generate_statement
from contexts.financial_gateway.application.statement_history import (
    FEE_FLOOR,
    FEE_FLOOR_BIC,
    HISTORY_PREFIX,
    build_history,
)
from contexts.financial_gateway.domain import camt053
from tests.test_statement_camt053 import _later, db, service  # noqa: F401 (fixtures)

_NOW = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
_SCHEMA = json.loads((pathlib.Path(__file__).parents[1] / "data" / "exceptions_schema.json")
                     .read_text())["validator"]["$jsonSchema"]


def _history(**kw):
    return build_history(now=_NOW, **kw)


def test_history_is_deterministic_for_a_seed():
    a, b = _history(), _history()
    strip = lambda docs: [{k: v for k, v in d.items() if k not in ("_id", "exceptionId",
                           "paymentMessageId", "payload", "rawMessage")} for d in docs]
    assert [len(s["entries"]) for s in a[0]] == [len(s["entries"]) for s in b[0]]
    assert [e["paymentId"] for e in a[1] if e["paymentId"].startswith(HISTORY_PREFIX)] == \
           [e["paymentId"] for e in b[1] if e["paymentId"].startswith(HISTORY_PREFIX)]
    assert len(strip(a[1])) == len(strip(b[1]))


def test_fourteen_chained_off_chain_statements_ending_before_today():
    statements, _ = _history()
    assert len(statements) == 14
    assert all(s["statement"]["historical"] is True for s in statements)
    for prev, nxt in zip(statements, statements[1:]):
        assert nxt["statement"]["window"]["from"] == prev["statement"]["window"]["to"]
        assert nxt["statement"]["openingBalance"] == prev["statement"]["closingBalance"]
    assert statements[-1]["statement"]["window"]["to"] <= _NOW


def test_the_mix_is_mostly_clean_with_every_lever_and_some_orphans():
    statements, _ = _history()
    outcomes = Counter(e["simulatedOutcome"] for s in statements for e in s["entries"])
    wires = sum(n for k, n in outcomes.items() if k != "ORPHAN")
    assert outcomes[camt053.CLEAN] / wires > 0.75
    for lever in (camt053.FEE_DEDUCTED, camt053.REFERENCE_ALTERED, camt053.LATE,
                  camt053.AMOUNT_TRANSPOSED, "ORPHAN"):
        assert outcomes[lever] >= 1, lever


def test_every_wire_is_booked_exactly_once_and_late_ones_a_day_late():
    statements, _ = _history()
    seen = Counter(e["simulatedPaymentId"] for s in statements for e in s["entries"]
                   if e["simulatedPaymentId"])
    assert set(seen.values()) == {1}
    for s in statements:
        for e in s["entries"]:
            if e["simulatedOutcome"] == camt053.LATE:
                assert e["settledAt"] < s["statement"]["window"]["from"]


def test_every_wire_line_is_matched_and_every_orphan_already_queued():
    """Only an orphan stays UNMATCHED (as a dismissed live one does), and it carries the
    `exceptionId` that makes `raise_orphans` skip it even without the historical filter."""
    statements, _ = _history()
    for e in (e for s in statements for e in s["entries"]):
        if e["simulatedOutcome"] == "ORPHAN":
            assert e["recon"]["exceptionId"]
        else:
            assert e["recon"]["status"] in ("AUTO_MATCHED", "MANUAL_MATCHED")


def test_the_gb_correspondent_has_enough_fee_precedents_including_a_debt_one():
    _, exceptions = _history()
    fees = [x for x in exceptions if x["historical"]["statementOutcome"] == camt053.FEE_DEDUCTED
            and x["historical"]["correspondentBic"] == FEE_FLOOR_BIC]
    assert len(fees) >= FEE_FLOOR
    assert any(x["historical"]["chargeBearer"] == "DEBT" for x in fees)


def test_every_precedent_resolution_follows_the_charge_bearer_rule():
    """Decision 2: DEBT → POST_ADJUSTMENT, anything else → ACCEPT. History must not teach the
    agent the opposite."""
    _, exceptions = _history()
    for x in exceptions:
        assert x["status"] == "RESOLVED"
        if x["historical"]["statementOutcome"] == camt053.FEE_DEDUCTED:
            want = "POST_ADJUSTMENT" if x["historical"]["chargeBearer"] == "DEBT" else "ACCEPT_DISCREPANCY"
            assert x["resolution"]["action"] == want


def test_a_rekeyed_line_closes_both_twins():
    _, exceptions = _history()
    missing = [x for x in exceptions if x["historical"]["statementOutcome"] == camt053.REFERENCE_ALTERED
               and x["category"] == "RECONCILIATION_MISSING"]
    orphaned = [x for x in exceptions if x["historical"]["statementOutcome"] == camt053.REFERENCE_ALTERED
                and x["category"] == "ORPHANED_SETTLEMENT"]
    assert missing and len(missing) == len(orphaned)
    assert {x["resolution"]["action"] for x in missing + orphaned} == {"LINK_STATEMENT_ENTRY"}


def test_exceptions_use_only_the_stub_enums():
    _, exceptions = _history()
    props = _SCHEMA["properties"]
    for x in exceptions:
        assert set(_SCHEMA["required"]) <= set(x)
        assert set(x) <= set(props), set(x) - set(props)
        assert x["category"] in props["category"]["enum"]
        assert x["resolution"]["action"] in props["resolution"]["properties"]["action"]["enum"]
        h = props["historical"]["properties"]
        assert x["historical"]["chargeBearer"] in h["chargeBearer"]["enum"]
        assert x["historical"]["statementOutcome"] in h["statementOutcome"]["enum"]


def test_history_written_to_the_db_does_not_disturb_live_generation(service, db):
    from tests.test_statement_camt053 import SETTLEMENT_ACCOUNT, _line, _settled_wire

    pid = _settled_wire(service, db)
    statements, _ = build_history(now=_later(0), account_code=SETTLEMENT_ACCOUNT)
    db["paymentMessages"].insert_many(statements)

    live = generate_statement(db, account_code=SETTLEMENT_ACCOUNT, include_orphan=False, now=_later(1))
    assert live["statement"]["sequence"] == 1
    assert _line(live, pid)
