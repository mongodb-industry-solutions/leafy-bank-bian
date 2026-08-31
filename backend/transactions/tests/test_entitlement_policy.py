"""Stage 2 entitlement policy — the pure functions, no database (doc 15 §3 step 2).

The policy table is the substitute for limit fields the canonical model does not have
(doc 15 B2, Doina Q14). Everything here is a decision the demo makes, so every threshold
is pinned: the day the table changes, this file says which scenario changed with it.
"""

import pytest

from contexts.party_authentication.domain import entitlement_policy as policy


# --- the table ---------------------------------------------------------------

@pytest.mark.parametrize("segment", policy.SEGMENTS)
def test_every_spec_segment_has_a_policy(segment):
    """`customers.segment` is a spec enum, so a missing entry is a silent fallback."""
    p = policy.policy_for(segment)
    assert p["perPaymentLimit"] > 0
    assert p["dualApprovalThreshold"] > 0
    assert p["stepUpThreshold"] > 0


def test_commercial_dual_approval_threshold_is_ten_thousand():
    """R9, exactly: "$25,000 -> Payment > $10,000 -> Second corporate approver required"."""
    assert policy.policy_for("COMMERCIAL")["dualApprovalThreshold"] == 10_000.0


def test_commercial_limit_admits_the_flagship_payment():
    assert policy.limit_available(25_000.0, "COMMERCIAL")


def test_an_unknown_segment_gets_the_tightest_policy_not_a_permissive_one():
    """A payment whose entitlement we cannot establish is not entitled."""
    assert policy.policy_for(None) == policy.policy_for("RETAIL")
    assert policy.policy_for("PLATINUM_WHATEVER") == policy.policy_for("RETAIL")


def test_env_overrides_a_single_threshold(monkeypatch):
    monkeypatch.setenv("ENTITLEMENT_DUAL_APPROVAL_THRESHOLD_COMMERCIAL", "50000")
    assert policy.policy_for("COMMERCIAL")["dualApprovalThreshold"] == 50_000.0
    assert policy.policy_for("RETAIL")["dualApprovalThreshold"] == 10_000.0, "unrelated"


def test_a_malformed_override_falls_back_to_the_stricter_default(monkeypatch):
    """A typo in a deploy config must not take the payment path down."""
    monkeypatch.setenv("ENTITLEMENT_PER_PAYMENT_LIMIT_RETAIL", "twenty thousand")
    assert policy.policy_for("RETAIL")["perPaymentLimit"] == 25_000.0


# --- limits (R7) -------------------------------------------------------------

@pytest.mark.parametrize("amount,allowed", [
    (24_999.99, True),
    (25_000.0, True),      # at the limit — inclusive
    (25_000.01, False),    # over
])
def test_retail_limit_boundary(amount, allowed):
    assert policy.limit_available(amount, "RETAIL") is allowed


# --- signatories (R5) --------------------------------------------------------

_SIGNATORIES = [
    {"customerId": "CUST-1", "type": "PRIMARY", "signingRule": "JOINT"},
    {"customerId": "CUST-2", "type": "JOINT", "signingRule": "JOINT"},
]


def test_signatory_for_finds_the_caller():
    assert policy.signatory_for(_SIGNATORIES, "CUST-2")["type"] == "JOINT"


@pytest.mark.parametrize("signatories", [None, [], _SIGNATORIES])
def test_a_non_signatory_gets_none_not_an_error(signatories):
    """None is the entitlement answer, not a failure to look it up."""
    assert policy.signatory_for(signatories, "CUST-9") is None


# --- dual approval (R8/R9) ---------------------------------------------------

@pytest.mark.parametrize("rule", ["JOINT", "ANY_TWO"])
def test_a_multi_signer_mandate_always_needs_a_second_approver(rule):
    """At any amount — that is what the signing rule means."""
    assert policy.approval_required(1.0, "COMMERCIAL", rule)


@pytest.mark.parametrize("amount,required", [
    (9_999.99, False),
    (10_000.0, False),     # at the threshold — "above" means above
    (10_000.01, True),
    (25_000.0, True),      # the flagship payment
])
def test_sole_mandate_needs_approval_only_above_the_threshold(amount, required):
    assert policy.approval_required(amount, "COMMERCIAL", "SOLE") is required


# --- step-up authentication (R1) ---------------------------------------------

@pytest.mark.parametrize("method", [None, "NONE"])
def test_an_absent_authentication_is_never_sufficient(method):
    """B1's rule: absent is not weak, it is nothing. Never a PASS."""
    assert not policy.authentication_sufficient(method, 0, 1.0, "RETAIL")


def test_a_single_weak_factor_carries_a_small_amount():
    assert policy.authentication_sufficient("PASSWORD", 1, 100.0, "RETAIL")


def test_a_single_weak_factor_does_not_carry_a_large_amount():
    assert not policy.authentication_sufficient("PASSWORD", 1, 20_000.0, "RETAIL")


@pytest.mark.parametrize("method", ["OTP", "BIOMETRIC", "MTLS"])
def test_a_strong_method_carries_a_large_amount_alone(method):
    assert policy.authentication_sufficient(method, 1, 20_000.0, "RETAIL")


def test_two_weak_factors_carry_a_large_amount():
    assert policy.authentication_sufficient("PASSWORD", 2, 20_000.0, "RETAIL")


# --- restrictions (R6) -------------------------------------------------------

_NOW = "2026-08-31T12:00:00+00:00"


@pytest.mark.parametrize("kind,blocks", [
    ("DEBIT_BLOCK", True),
    ("FULL_BLOCK", True),
    ("LEGAL_HOLD", True),
    ("CREDIT_BLOCK", False),   # blocks money IN, not out
])
def test_which_restrictions_block_a_debit(kind, blocks):
    found = policy.blocking_restrictions([{"type": kind}], now=_NOW, side="DEBIT")
    assert bool(found) is blocks


def test_a_lapsed_restriction_does_not_block():
    lapsed = [{"type": "DEBIT_BLOCK", "expiresAt": "2026-01-01"}]
    assert policy.blocking_restrictions(lapsed, now=_NOW) == []


def test_a_restriction_expiring_in_the_future_still_blocks():
    live = [{"type": "DEBIT_BLOCK", "expiresAt": "2027-01-01"}]
    assert len(policy.blocking_restrictions(live, now=_NOW)) == 1


def test_credit_block_does_block_the_credit_side():
    """The side is a parameter, so the creditor-side evaluation a later stage needs is
    already here rather than a copy of this function."""
    assert policy.blocking_restrictions([{"type": "CREDIT_BLOCK"}], now=_NOW, side="CREDIT")


@pytest.mark.parametrize("restrictions", [None, []])
def test_no_restrictions_is_the_common_case(restrictions):
    assert policy.blocking_restrictions(restrictions, now=_NOW) == []
