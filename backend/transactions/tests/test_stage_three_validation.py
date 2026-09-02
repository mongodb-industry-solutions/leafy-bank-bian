"""Stage 3 step 3 — the nine recorded checks, and the two new refusals.

Doc 17 §3 step 3's gate, as tests. Four groups:

1. **`identifier_format`** (R8) — BIC / IBAN / ABA shape and checksums, pure.
2. **`rail_viability`** (R10) — the type/rail matrix, with its keys and values asserted
   against the canonical spec's own `enum` arrays (defect 2026-04-28: never hand-rolled).
3. **`validation.run` behaviour** — all nine checks present, in order, with truthful modes;
   one test per refusal path.
4. **Fixture fidelity** — the identifiers the fixtures supply must themselves satisfy the
   rules under test, or every acceptance assertion is vacuous.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexts.payment_order_initiation.domain import (
    bank_identity,
    duplicate_detection,
    identifier_format,
    rail_viability,
)

# `propose_payments.json` lives in the umbrella docs directory, one level ABOVE the repo:
# tests -> transactions -> backend -> leaf-bank-bian -> payment-flow-full.
SPEC = (
    Path(__file__).resolve().parents[4] / "doinas-research" / "propose_payments.json"
)

STAGE = "3 validate"

# The ten checks her L434-L439 + L462 name, in the order `validation.run` records them.
# `duplicate_detection` sits fourth because her L435 groups it with structural validation
# ("Required fields present / Currency valid / Amount valid / Duplicate detection"), not at
# the end.
EXPECTED_CHECKS = [
    "required_fields",
    "currency_valid",
    "amount_valid",
    "duplicate_detection",
    "payment_type_viable",
    "currency_consistent",
    "beneficiary_recognised",
    "account_format_valid",
    "domestic_or_crossborder",
    "funds_available",
]


# --------------------------------------------------------------------------- #
# 1. identifier_format (R8)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bic", ["LEAFUS33", "CHASUS33", "BARCGB22", "DEUTDEFFXXX"])
def test_well_formed_bics_are_accepted(bic):
    assert identifier_format.bic_problem(bic) is None


@pytest.mark.parametrize(
    "bic,because",
    [
        ("CHASUS3", "8 or 11"),          # 7 chars
        ("CHASUS333", "8 or 11"),        # 9 — the classic paste error
        ("CHASUS3333", "8 or 11"),       # 10
        ("1HASUS33", "ISO 9362"),        # digit in the institution code
        ("CHAS1S33", "ISO 9362"),        # digit in the country code
    ],
)
def test_malformed_bics_are_reported_with_a_reason(bic, because):
    problem = identifier_format.bic_problem(bic)
    assert problem and because in problem


@pytest.mark.parametrize("iban", ["GB33BUKB20201555555555", "DE75512108001245126199"])
def test_valid_ibans_are_accepted(iban):
    assert identifier_format.iban_problem(iban) is None


def test_iban_spaces_are_tolerated():
    """Printed IBANs are grouped in fours; a pasted one arrives with spaces."""
    assert identifier_format.iban_problem("GB33 BUKB 2020 1555 5555 55") is None


def test_iban_with_transposed_digits_fails_the_check_digits():
    """The point of mod-97: a shape-only check passes almost any typo."""
    problem = identifier_format.iban_problem("GB33BUKB20201555555554")
    assert problem and "mod-97" in problem


def test_iban_with_a_bad_shape_is_reported_before_the_check_digits():
    problem = identifier_format.iban_problem("G33BUKB20201555555555")
    assert problem and "ISO 13616" in problem


@pytest.mark.parametrize("aba", ["021000021", "121000248", "021000089"])
def test_valid_abas_are_accepted(aba):
    assert identifier_format.aba_problem(aba) is None


def test_aba_with_a_bad_checksum_is_reported():
    problem = identifier_format.aba_problem("021000022")
    assert problem and "mod-10" in problem


@pytest.mark.parametrize("aba", ["02100002", "0210000211", "02100002X"])
def test_aba_must_be_nine_digits(aba):
    problem = identifier_format.aba_problem(aba)
    assert problem and "9 digits" in problem


@pytest.mark.parametrize(
    "validator",
    [identifier_format.bic_problem, identifier_format.iban_problem,
     identifier_format.aba_problem],
)
@pytest.mark.parametrize("empty", [None, ""])
def test_absent_identifiers_are_not_a_problem(validator, empty):
    """An unenriched payment legitimately has `creditor.bic: None`.

    Requiredness is the request contract's job; this module answers shape only.
    """
    assert validator(empty) is None


def test_clearing_member_is_checksum_validated_only_for_usaba():
    """The honest gap: no sort-code / BLZ rule is implemented, so none is claimed."""
    assert identifier_format.clearing_member_problem("USABA", "021000022") is not None
    assert identifier_format.clearing_member_problem("GBDSC", "021000022") is None
    assert identifier_format.clearing_member_is_validated("USABA") is True
    assert identifier_format.clearing_member_is_validated("GBDSC") is False


# --------------------------------------------------------------------------- #
# 2. rail_viability (R10)
# --------------------------------------------------------------------------- #

def _spec_enum(field: str) -> list[str]:
    if not SPEC.exists():
        pytest.skip("propose_payments.json not available in this checkout")
    schema = json.loads(SPEC.read_text())["collections"]["payments"]["validator"]["$and"][0]
    return schema["$jsonSchema"]["properties"][field]["enum"]


def test_the_matrix_covers_every_rail_in_the_spec_enum():
    """A rail with no row would be reported as unknown and refuse every payment on it."""
    assert set(rail_viability.VIABLE_TYPES_BY_RAIL) == set(_spec_enum("rail"))


def test_every_type_in_the_matrix_is_a_spec_enum_value():
    """Defect 2026-04-28: enum values are sourced from the spec, never hand-rolled."""
    allowed = set(_spec_enum("type"))
    for rail, types in rail_viability.VIABLE_TYPES_BY_RAIL.items():
        assert types <= allowed, f"{rail}: {types - allowed}"


def test_every_spec_type_is_viable_on_at_least_one_rail():
    """A type no rail carries is unreachable — either the matrix or the enum is wrong."""
    carried = set().union(*rail_viability.VIABLE_TYPES_BY_RAIL.values())
    assert set(_spec_enum("type")) <= carried


@pytest.mark.parametrize(
    "rail,payment_type",
    [
        ("INTERNAL", "CREDIT_TRANSFER"),   # the demo's regression path
        ("INTERNAL", "STANDING_ORDER"),
        ("WIRE", "CREDIT_TRANSFER"),       # the flagship path
        ("ACH", "DIRECT_DEBIT"),
        ("CARD", "CARD_PAYMENT"),
        ("RTP", "RTP"),
    ],
)
def test_viable_pairings_pass(rail, payment_type):
    assert rail_viability.viability_problem(rail, payment_type) is None


@pytest.mark.parametrize(
    "rail,payment_type",
    [
        ("INTERNAL", "CARD_PAYMENT"),   # storable before this stage; describes nothing
        ("WIRE", "DIRECT_DEBIT"),       # a wire cannot pull funds
        ("CARD", "CREDIT_TRANSFER"),
        ("INTERNAL", "DIRECT_DEBIT"),   # no mandate model in the demo
    ],
)
def test_non_viable_pairings_are_reported(rail, payment_type):
    problem = rail_viability.viability_problem(rail, payment_type)
    assert problem and "not viable" in problem


def test_an_unknown_rail_is_reported_rather_than_passed():
    problem = rail_viability.viability_problem("CHEQUE", "CREDIT_TRANSFER")
    assert problem and "not a known payment rail" in problem


def test_phase_1_rails_are_the_two_the_demo_executes():
    assert rail_viability.PHASE_1_RAILS == {"INTERNAL", "WIRE"}
    assert rail_viability.is_phase_1("WIRE") and not rail_viability.is_phase_1("ACH")


# --------------------------------------------------------------------------- #
# 3. validation.run behaviour
# --------------------------------------------------------------------------- #
#
# Driven through the real `PaymentsService`, reusing test_payments_service's fixtures — the
# saga is what orders the checks, and a unit test of `validation.run` alone would not catch a
# stage that never gets dispatched.

from test_payments_service import (  # noqa: E402  (fixtures must import after the helpers)
    CREDITOR,
    CUST_C,
    CUST_D,
    DEBTOR,
    FakeCollection,
    FakeConnection,
    _account,
    _checks,
    _initiate,
    _one,
    db,          # noqa: F401  (pytest fixture)
    service,     # noqa: F401  (pytest fixture)
)
from services.payments_service import PaymentsService  # noqa: E402


def test_stage_three_records_its_ten_checks_in_order(service, db):
    _initiate(service)
    recorded = _checks(db, stage=STAGE)
    assert [c["name"] for c in recorded] == EXPECTED_CHECKS


def test_every_stage_three_check_declares_a_mode(service, db):
    """R11 — Doina asks that the demo make clear which checks are sync and which async."""
    _initiate(service)
    recorded = _checks(db, stage=STAGE)
    assert recorded
    for entry in recorded:
        assert entry["mode"] in ("SYNC", "ASYNC"), entry


def test_an_internal_transfer_passes_every_stage_three_check(service, db):
    _initiate(service)
    for entry in _checks(db, stage=STAGE):
        assert entry["result"] == "PASS", entry


def test_the_corridor_check_reports_domestic_for_an_internal_transfer(service, db):
    _initiate(service)
    entry = _one(db, "domestic_or_crossborder")
    assert entry["result"] == "PASS"
    assert "DOMESTIC" in entry["detail"]
    assert bank_identity.OUR_BANK_COUNTRY in entry["detail"]


def test_a_non_viable_type_is_refused_and_names_the_check(db):
    """R10 — previously `payment_type` was accepted and never compared to `rail`."""
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)
    with pytest.raises(ValueError, match="not viable on rail"):
        _initiate(svc, payment_type="CARD_PAYMENT")

    entry = _one(db, "payment_type_viable")
    assert entry["result"] == "FAIL" and entry["stage"] == STAGE


def test_insufficient_funds_is_refused_and_reports_both_numbers(db):
    # `db["accounts"]` must be replaced BEFORE the service is constructed — PaymentsService
    # caches the collection handles in `__init__`, so a later swap is invisible to it.
    db["accounts"] = FakeCollection([
        _account(DEBTOR, CUST_D, available=10.0),
        _account(CREDITOR, CUST_C),
    ])
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(svc, instructed_amount=500.0)

    entry = _one(db, "funds_available")
    assert entry["result"] == "FAIL"
    assert "10.00" in entry["detail"] and "500.00" in entry["detail"]


def test_a_closed_creditor_account_is_refused_at_the_beneficiary_check(db):
    db["accounts"] = FakeCollection([
        _account(DEBTOR, CUST_D),
        _account(CREDITOR, CUST_C, status="CLOSED"),
    ])
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)
    with pytest.raises(ValueError, match="Creditor account is CLOSED"):
        _initiate(svc)

    assert _one(db, "beneficiary_recognised")["result"] == "FAIL"


def test_a_self_transfer_is_refused_at_the_beneficiary_check(db):
    svc = PaymentsService(FakeConnection(db), "leafy_bank_bian", payment_limit_usd=50_000.0)
    with pytest.raises(ValueError, match="must differ"):
        _initiate(svc, creditor_account_ref=DEBTOR)

    assert _one(db, "beneficiary_recognised")["result"] == "FAIL"


def test_the_format_check_validates_the_fixture_identifiers(service, db):
    """A PASS must name what it validated, or the check is decorative."""
    _initiate(service)
    entry = _one(db, "account_format_valid")
    assert entry["result"] == "PASS"
    assert "our BIC" in entry["detail"]
    assert "IBAN" in entry["detail"]


# --------------------------------------------------------------------------- #
# 3b. Duplicate detection (step 4, R4 / B4)
# --------------------------------------------------------------------------- #

def test_the_duplicate_filter_excludes_the_payment_being_validated():
    """Without this every payment matches itself, because stage 1 persists it first.

    Doc 11 §11: the instruction is written at DRAFT before validation runs.
    """
    flt = duplicate_detection.recent_duplicate_filter(
        debtor_account_id=DEBTOR, creditor_account_no="82829931",
        instructed_amount=250.0, instructed_currency="USD",
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        exclude_payment_id="PAY-me",
    )
    assert flt["paymentId"] == {"$ne": "PAY-me"}


def test_the_duplicate_filter_ignores_payments_that_moved_no_money():
    """Resembling a REJECTED payment is not a warning worth raising."""
    flt = duplicate_detection.recent_duplicate_filter(
        debtor_account_id=DEBTOR, creditor_account_no="82829931",
        instructed_amount=250.0, instructed_currency="USD",
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        exclude_payment_id="PAY-me",
    )
    excluded = set(flt["status"]["$nin"])
    assert {"REJECTED", "FAILED", "CANCELLED"} <= excluded


def test_the_duplicate_window_is_read_lazily_from_the_environment(monkeypatch):
    """Module-scope `os.getenv` runs before `load_dotenv` (defects.md 2026-06-30)."""
    assert duplicate_detection.window_seconds() == 900
    monkeypatch.setenv("DUPLICATE_WINDOW_SECONDS", "60")
    assert duplicate_detection.window_seconds() == 60


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
def test_a_malformed_duplicate_window_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("DUPLICATE_WINDOW_SECONDS", bad)
    assert duplicate_detection.window_seconds() == 900


def test_the_window_bounds_the_filter(monkeypatch):
    monkeypatch.setenv("DUPLICATE_WINDOW_SECONDS", "600")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    flt = duplicate_detection.recent_duplicate_filter(
        debtor_account_id=DEBTOR, creditor_account_no="82829931",
        instructed_amount=250.0, instructed_currency="USD",
        now=now, exclude_payment_id="PAY-me",
    )
    assert flt["createdAt"]["$gte"] == now - timedelta(seconds=600)


def test_a_first_payment_records_a_passing_duplicate_check(service, db):
    _initiate(service)
    entry = _one(db, "duplicate_detection")
    assert entry["result"] == "PASS"


def test_the_duplicate_warning_names_the_payment_it_resembles(service, db):
    first = _initiate(service)
    _initiate(service)

    payments = db["payments"].docs
    second_doc = payments[-1]
    entry = next(
        c for c in second_doc["checks"]
        if c["name"] == "duplicate_detection" and c["stage"] == STAGE
    )
    assert entry["result"] == "WARN"
    assert first["paymentId"] in entry["detail"]
    # The point of WARN over FAIL: the payment went through.
    assert second_doc["status"] == "SETTLED"


def test_a_different_amount_is_not_a_duplicate(service, db):
    _initiate(service)
    _initiate(service, instructed_amount=251.0)

    second_doc = db["payments"].docs[-1]
    entry = next(
        c for c in second_doc["checks"]
        if c["name"] == "duplicate_detection" and c["stage"] == STAGE
    )
    assert entry["result"] == "PASS"


# The surfaces that BUILD an initiate payload. `api_models.py` is excluded on purpose: the
# request contract has declared `idempotencyKey` since stage 1 (R7), and declaring a field
# nobody populates is exactly the state this test protects.
_CALLER_GLOBS = (
    "frontend/components/**/*.js",
    "frontend/lib/**/*.js",
    "frontend/app/**/*.js",
    "backend/ledger/static/*.html",
)


def test_no_caller_sends_an_idempotency_key():
    """The standing precondition on the deferred Atlas index (doc 17 B4, _state.md).

    Until `idx_idempotency_key_unique` exists on Atlas, a caller sending a key would make
    `capture.py`'s `find_one` pre-check dedupe sequential retries and LOOK correct, while two
    concurrent requests with the same key both moved money — a failure that appears only
    under real concurrency. Step 4 adds duplicate detection WITHOUT crossing that line, and
    this test is what keeps it uncrossed.

    If this fails, the fix is not to delete the test: apply the index in the same change.
    """
    repo = Path(__file__).resolve().parents[3]
    offenders = []
    for glob in _CALLER_GLOBS:
        for path in repo.glob(glob):
            if {"node_modules", ".next"} & set(path.parts):
                continue
            text = path.read_text(errors="ignore")
            # `Idempotency-Key` is the header; `idempotencyKey:` is the object-literal key
            # in a request payload. A bare `.idempotencyKey` property READ is not a match on
            # purpose — the GL monitor displays `ledgerEvents.idempotencyKey`, which is the
            # LEDGER's own key on a different collection and has nothing to do with the
            # payments index this test protects.
            for needle in ("Idempotency-Key", "idempotencyKey:"):
                if needle in text:
                    offenders.append(f"{path.relative_to(repo)} :: {needle}")
    assert not offenders, (
        "a caller now sends an idempotency key — apply idx_idempotency_key_unique on Atlas "
        f"in the same change: {offenders}"
    )


# --------------------------------------------------------------------------- #
# 4. Fixture fidelity
# --------------------------------------------------------------------------- #

def test_fixture_ibans_are_mod97_valid():
    """The fixtures' own identifiers must satisfy the rule under test.

    Doc 17 §4: `_account()` had no `iban` at all, so every party snapshot came out
    `iban: None` and the format path was never exercised. Having added them, they must be
    genuinely valid — otherwise stage 3 would refuse every fixture payment.
    """
    import test_payments_service as fixtures

    candidates = list(fixtures._IBANS.values()) + [fixtures._DEFAULT_IBAN]
    assert candidates
    for iban in candidates:
        assert identifier_format.iban_problem(iban) is None, iban


def test_fixture_accounts_carry_a_distinct_iban():
    """DEBTOR and CREDITOR both end in "01"; a suffix-keyed table gave them the same IBAN."""
    import test_payments_service as fixtures

    debtor = fixtures._account(DEBTOR, CUST_D)
    creditor = fixtures._account(CREDITOR, CUST_C)
    assert debtor["iban"] and creditor["iban"]
    assert debtor["iban"] != creditor["iban"]


def test_fixture_customers_carry_the_fields_party_snapshot_reads():
    """`identification.legalName` and `contact.addresses[]` — both previously absent."""
    import test_payments_service as fixtures

    customer = fixtures._customer(CUST_D)
    assert customer["identification"]["legalName"]
    assert customer["contact"]["addresses"][0]["line1"]


def test_our_own_identifiers_satisfy_the_rules_we_enforce():
    """A typo in `bank_identity` would otherwise reach a rail message silently."""
    assert identifier_format.bic_problem(bank_identity.OUR_BIC) is None
    assert identifier_format.aba_problem(bank_identity.OUR_ABA) is None
