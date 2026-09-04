"""BIAN PartyAuthentication (SD 38917) — Evaluate, and the token it issues.

Level 1: the token is real (signed, expiring, server-issued); the credential is not
(selecting a persona IS the credential). These tests pin exactly that boundary, so nobody
later reads a passing suite as proof that authentication is implemented.
"""

import jwt
import pytest

from services.party_authentication_service import PartyAuthenticationService
from shared import party_authentication as party_auth


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, flt, projection=None):
        return next((d for d in self.docs if d["customerId"] == flt["customerId"]), None)


class FakeConnection:
    def __init__(self, docs):
        self._db = {"customers": FakeCollection(docs)}

    def get_database(self, _name):
        return self._db


ACTIVE = {"customerId": "CUST-1", "status": "ACTIVE", "type": "INDIVIDUAL", "segment": "RETAIL"}
SUSPENDED = {"customerId": "CUST-2", "status": "SUSPENDED"}


@pytest.fixture
def service():
    return PartyAuthenticationService(FakeConnection([ACTIVE, SUSPENDED]), "leafy_bank_bian")


def _claims(token):
    return jwt.decode(token, party_auth._DEV_SECRET, algorithms=[party_auth.ALGORITHM],
                      audience=party_auth.AUDIENCE, issuer=party_auth.ISSUER)


# --- what Evaluate verifies ---------------------------------------------------

def test_an_active_customer_gets_a_token(service):
    issued = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    claims = _claims(issued["token"])
    assert claims["sub"] == "CUST-1"
    assert claims["callerType"] == "CUSTOMER"
    assert claims["sessionRef"].startswith("SESS-")


@pytest.mark.parametrize("party", ["CUST-2", "CUST-does-not-exist"])
def test_a_customer_who_is_not_active_or_does_not_exist_is_refused(service, party):
    """One refusal for both, deliberately — a caller must not be able to probe which
    customer ids exist. The router maps this to 401, never 404."""
    with pytest.raises(ValueError, match="could not be authenticated"):
        service.evaluate(party_reference=party, caller_type="CUSTOMER")


def test_an_operator_needs_no_customer_record(service):
    """Back-office personas are staff, not customers — there is nothing to look up, which
    is exactly why their token is marked OPERATOR rather than passed off as a customer's."""
    issued = service.evaluate(party_reference="OPS-Harry", caller_type="OPERATOR")
    assert _claims(issued["token"])["callerType"] == "OPERATOR"


# --- what the assessment says about itself -----------------------------------

def test_the_assessment_admits_no_credential_was_verified(service):
    """The one assertion that must never start passing by accident. If `credentialVerified`
    ever becomes True, a real credential check landed — and this test should be rewritten,
    not deleted."""
    issued = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    assert issued["assessment"]["credentialVerified"] is False
    assert issued["assessment"]["method"] == "PASSWORD"
    assert issued["assessment"]["factorCount"] == 1


# --- the token itself --------------------------------------------------------

def test_the_token_expires(service, monkeypatch):
    monkeypatch.setenv(party_auth.TTL_ENV, "60")
    issued = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    claims = _claims(issued["token"])
    assert claims["exp"] - claims["iat"] == 60


def test_a_malformed_ttl_falls_back_to_the_default(service, monkeypatch):
    monkeypatch.setenv(party_auth.TTL_ENV, "one hour")
    claims = _claims(service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")["token"])
    assert claims["exp"] - claims["iat"] == party_auth._DEFAULT_TTL_SECONDS


def test_the_token_is_signed_and_cannot_be_edited(service):
    """The point of the whole change: a browser can no longer decide who it is."""
    token = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")["token"]
    # 32+ bytes, or pyjwt warns about the key length and the point of the test is lost
    # in the noise.
    forged = jwt.encode({**_claims(token), "sub": "CUST-2"},
                        "an-attackers-secret-of-a-respectable-length",
                        algorithm=party_auth.ALGORITHM)
    with pytest.raises(jwt.InvalidSignatureError):
        _claims(forged)


def test_an_unknown_caller_type_is_refused():
    with pytest.raises(ValueError, match="unknown callerType"):
        party_auth.issue(subject="CUST-1", caller_type="ROBOT", method="PASSWORD", factor_count=1)


# --- Question/Evaluate: the step-up factor ------------------------------------
#
# These exist because the suite previously could not have caught the gap they close.
# `test_payments_service._ASSERTION` hand-writes `{"method": "OTP", "factorCount": 2}`, so
# every stage-2 test above the step-up threshold passed while the live system could only
# ever produce PASSWORD/1 — the flagship $25,000 corporate payment was unreachable through
# the UI. A fixture asserting a capability the running system does not have is the same
# class of blind spot as the write-path audit gap (umbrella defects.md 2026-05-04).


@pytest.fixture
def stepped_up(service):
    """A Level 1 session, plus the correct code for it."""
    issued = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    ref = issued["assessment"]["sessionRef"]
    return issued, ref, party_auth.challenge_code(ref, "otp")


def test_level_one_is_a_single_weak_factor(service):
    issued = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    assert issued["assessment"]["method"] == "PASSWORD"
    assert issued["assessment"]["factorCount"] == 1
    assert issued["assessment"]["stepUp"] is False


def test_the_step_up_raises_the_session_to_two_factors(service, stepped_up):
    issued, ref, code = stepped_up
    upgraded = service.evaluate_question(
        token=issued["token"], party_authentication_id=ref, challenge_response=code)

    assert upgraded["assessment"]["method"] == "OTP"
    assert upgraded["assessment"]["factorCount"] == 2
    assert upgraded["assessment"]["stepUp"] is True
    claims = _claims(upgraded["token"])
    assert claims["stepUp"] is True
    assert claims["factorCount"] == 2


def test_the_step_up_preserves_who_the_party_is(service, stepped_up):
    """The whole point of anchoring to the token: a step-up may not change the subject."""
    issued, ref, code = stepped_up
    upgraded = service.evaluate_question(
        token=issued["token"], party_authentication_id=ref, challenge_response=code)

    assert _claims(upgraded["token"])["sub"] == "CUST-1"
    assert _claims(upgraded["token"])["callerType"] == "CUSTOMER"


def test_the_step_up_issues_a_new_session_ref(service, stepped_up):
    """The assessment is immutable and unpersisted, so the stronger one is a fresh issue."""
    issued, ref, code = stepped_up
    upgraded = service.evaluate_question(
        token=issued["token"], party_authentication_id=ref, challenge_response=code)
    assert upgraded["assessment"]["sessionRef"] != ref


def test_a_wrong_code_is_refused(service, stepped_up):
    """A challenge that accepted anything could not demonstrate a FAILED step-up."""
    issued, ref, _ = stepped_up
    with pytest.raises(ValueError, match="Challenge response is incorrect"):
        service.evaluate_question(
            token=issued["token"], party_authentication_id=ref, challenge_response="000000")


def test_a_code_cannot_be_replayed_onto_another_session(service, stepped_up):
    """The code is an HMAC over the session ref, so it is bound to one session."""
    issued, ref, code = stepped_up
    other = service.evaluate(party_reference="CUST-1", caller_type="CUSTOMER")
    other_ref = other["assessment"]["sessionRef"]

    assert party_auth.challenge_code(other_ref, "otp") != code
    with pytest.raises(ValueError, match="Challenge response is incorrect"):
        service.evaluate_question(
            token=other["token"], party_authentication_id=other_ref, challenge_response=code)


def test_a_token_for_a_different_session_is_refused(service, stepped_up):
    issued, ref, code = stepped_up
    with pytest.raises(ValueError, match="missing, expired or invalid"):
        service.evaluate_question(
            token=issued["token"], party_authentication_id="SESS-SOMEONEELSE",
            challenge_response=code)


def test_a_step_up_without_a_valid_token_is_refused(service, stepped_up):
    """Without the anchor, step-up would mint a two-factor assessment for anyone."""
    _, ref, code = stepped_up
    with pytest.raises(ValueError, match="missing, expired or invalid"):
        service.evaluate_question(
            token="not.a.real.token", party_authentication_id=ref, challenge_response=code)


def test_the_step_up_still_admits_no_credential_was_verified(service, stepped_up):
    """Two factors, and still a simulation: the code is shown on the screen that answers it."""
    issued, ref, code = stepped_up
    upgraded = service.evaluate_question(
        token=issued["token"], party_authentication_id=ref, challenge_response=code)

    assert upgraded["assessment"]["credentialVerified"] is False
    assert upgraded["assessment"]["simulated"] is True


def test_the_challenge_is_retrievable_and_matches_what_evaluate_expects(service, stepped_up):
    issued, ref, code = stepped_up
    question = service.retrieve_question(token=issued["token"], party_authentication_id=ref)

    assert question["challengeCode"] == code
    assert question["deliveryChannel"] == "ON_SCREEN_SIMULATION"
    assert question["simulated"] is True


def test_the_step_up_satisfies_the_policy_that_refused_level_one():
    """The end-to-end claim, asserted against the policy rather than a hand-written fixture.

    COMMERCIAL step-up threshold is 10,000; the flagship payment is 25,000. This is the
    exact pair that was unsatisfiable before Question/Evaluate existed.
    """
    import sys
    from pathlib import Path
    txn = Path(__file__).resolve().parents[2] / "transactions"
    sys.path.insert(0, str(txn))
    try:
        from contexts.party_authentication.domain import entitlement_policy as policy
    finally:
        sys.path.remove(str(txn))

    assert policy.authentication_sufficient("PASSWORD", 1, 25_000.0, "COMMERCIAL") is False
    assert policy.authentication_sufficient("OTP", 2, 25_000.0, "COMMERCIAL") is True
