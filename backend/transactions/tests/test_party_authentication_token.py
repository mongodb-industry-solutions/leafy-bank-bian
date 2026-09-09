"""The verifying side: `shared/party_authentication_token.py`.

What changed with the token, in one sentence: `customerId` was a body field a browser could
type anything into, and the ownership check compared that claimed string against the account
it named — circular. Now identity comes from a signature the bank produced.

These tests pin the two rules that make that true (a customer token cannot initiate for
another customer; an operator token can), the rollout flag, and the fact that an absent
token still yields the honest SKIP rather than a pass.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from shared import party_authentication_token as party_auth

SECRET = party_auth._DEV_SECRET


def _token(*, sub="CUST-1", caller_type="CUSTOMER", ttl=3600, secret=SECRET,
           issuer=party_auth.ISSUER, audience=party_auth.AUDIENCE, method="PASSWORD",
           factor_count=1):
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": sub, "callerType": caller_type, "method": method,
        "factorCount": factor_count, "sessionRef": "SESS-ABC123",
        "authenticatedAt": now.isoformat(), "iss": issuer, "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }, secret, algorithm=party_auth.ALGORITHM)


def _header(token):
    return f"Bearer {token}"


# --- the header --------------------------------------------------------------

@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   ", "abc"])
def test_a_header_that_carries_no_bearer_token_reads_as_absent(header):
    assert party_auth.bearer_token(header) is None


def test_the_scheme_is_case_insensitive():
    assert party_auth.bearer_token("bearer abc") == "abc"


# --- no token ----------------------------------------------------------------

def test_without_a_token_the_body_stands_and_the_assertion_is_absent():
    """No token: `resolve_identity` returns only `customer_ref` so the body's fallback
    assertion (if any) survives the `**{**_initiate_kwargs(body), **identity}` merge at the
    caller. Returning `authentication: None` here used to clobber that fallback (defects.md
    2026-09-08, discriminator-conflation class). Stage 2 still records `method: NONE` and a
    SKIP when no assertion was supplied either — never a PASS."""
    identity = party_auth.resolve_identity(None, "CUST-9")
    assert identity == {"customer_ref": "CUST-9"}


def test_the_body_authentication_survives_the_route_merge_when_no_token():
    """The initiate route composes `{**_initiate_kwargs(body), **identity}`. With no token,
    `resolve_identity` omits `authentication`, so a body-supplied fallback assertion must
    survive that spread and reach stage 2 — the property Fix #1 restored. The resolver test
    above pins that `identity` has no `authentication` key; this pins the composition that
    the route actually performs, so a regression to `authentication: None` in the resolver
    would be caught here as a clobber, not just as a key-shape change."""
    body_authentication = {"method": "PASSWORD", "factorCount": 1}
    identity = party_auth.resolve_identity(None, "CUST-9")
    merged = {**{"authentication": body_authentication}, **identity}
    assert merged["authentication"] == body_authentication


def test_with_the_flag_on_a_missing_token_is_refused(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    with pytest.raises(party_auth.AuthenticationError, match="required"):
        party_auth.resolve_identity(None, "CUST-9")


@pytest.mark.parametrize("value,required", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("no", False), ("", False), ("maybe", False),
])
def test_the_rollout_flag_reads_conservatively(monkeypatch, value, required):
    """Anything that is not plainly affirmative leaves authentication optional — a typo in
    a deploy config must not start 401ing every payment."""
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", value)
    assert party_auth.require_authentication() is required


# --- a customer token --------------------------------------------------------

def test_a_customer_token_yields_a_verified_assertion():
    identity = party_auth.resolve_identity(_header(_token()), "CUST-1")
    assert identity["customer_ref"] == "CUST-1"
    assert identity["authentication"] == {
        "method": "PASSWORD", "factorCount": 1, "sessionRef": "SESS-ABC123",
        "authenticatedAt": identity["authentication"]["authenticatedAt"],
        "callerType": "CUSTOMER",
        # False for a Level 1 token, which is what this one is. True only after
        # PartyAuthentication Question/Evaluate raised the session.
        "stepUp": False,
    }


def test_a_customer_cannot_initiate_for_another_customer():
    """The hole the token closes. Before this, the body decided who you were."""
    with pytest.raises(party_auth.AuthenticationError, match="another customer"):
        party_auth.resolve_identity(_header(_token(sub="CUST-1")), "CUST-2")


# --- an operator token -------------------------------------------------------

def test_an_operator_may_initiate_for_a_customer():
    """A back-office operator legitimately initiates for someone else, and is recorded as
    having done so — `callerType` lands on `payments.authentication`."""
    identity = party_auth.resolve_identity(
        _header(_token(sub="OPS-Harry", caller_type="OPERATOR")), "CUST-2")
    assert identity["customer_ref"] == "CUST-2"
    assert identity["authentication"]["callerType"] == "OPERATOR"
    assert identity["authentication"]["sessionRef"] == "SESS-ABC123"


# --- a bad token -------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"secret": "a-different-secret-of-respectable-length"},   # forged
    {"ttl": -60},                                             # expired
    {"issuer": "somebody-else"},                              # wrong issuer
    {"audience": "another-app"},                              # wrong audience
])
def test_every_bad_token_is_refused_with_the_same_message(kwargs):
    """One message for all of them on purpose: a caller learning *why* its token failed is
    an oracle, and there is nothing it could do differently with the detail."""
    with pytest.raises(party_auth.AuthenticationError, match="missing, expired or invalid"):
        party_auth.resolve_identity(_header(_token(**kwargs)), "CUST-1")


def test_a_token_missing_a_required_claim_is_refused():
    thin = jwt.encode({"sub": "CUST-1", "aud": party_auth.AUDIENCE,
                       "iss": party_auth.ISSUER}, SECRET, algorithm=party_auth.ALGORITHM)
    with pytest.raises(party_auth.AuthenticationError):
        party_auth.resolve_identity(_header(thin), "CUST-1")


def test_a_present_but_invalid_token_is_refused_even_with_the_flag_off():
    """`REQUIRE_AUTHENTICATION=false` means "a token is optional", never "a bad token is
    acceptable". Falling back to the body here would make the flag a bypass."""
    with pytest.raises(party_auth.AuthenticationError):
        party_auth.resolve_identity(_header(_token(ttl=-60)), "CUST-1")
