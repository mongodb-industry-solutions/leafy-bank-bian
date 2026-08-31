"""The PartyAuthentication token contract — verifying side.

Counterpart to `accounts/shared/party_authentication.py`, which signs what this verifies.
The two share **configuration, not code**: the algorithm, issuer, audience, claim names and
secret env var are declared in both files and named as a pair. Neither imports the other,
and neither is a byte-identical mirror — mirroring a module across two Poetry projects is
how `bian-alias-map.json` drift happens (umbrella defects.md). A library call plus four
constants can diverge only in configuration, and that fails loudly as a signature or
audience error rather than quietly as wrong behaviour.

## Why the token wins over the request body

`customerId` is still on the request contract, and five callers send it. When a valid token
is present, **the token decides** and the body value is only allowed to agree with it:

  * `callerType: CUSTOMER` — the body's `customerId` must equal the token's `sub`, or the
    request is refused. A customer cannot initiate a payment as somebody else, which is
    precisely what the pre-token demo allowed.
  * `callerType: OPERATOR` / `API` — the body's `customerId` is honoured. A back-office
    operator legitimately initiates for a customer, and is recorded as having done so.

`REQUIRE_AUTHENTICATION` decides what happens when no token is presented at all. It
defaults to **false**, so every existing caller keeps working while the UI rolls out; flip
it to true once they all send one, and an unauthenticated initiate becomes a 401. Removing
`authentication{}` from the request contract is the last step of that rollout — with
`extra="forbid"`, removing a field breaks any caller still sending it, so it goes after the
flag flips, not before.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import jwt

logger = logging.getLogger(__name__)

# Shared with accounts/shared/party_authentication.py — change both together.
ALGORITHM = "HS256"
ISSUER = "leafy-bank-accounts"
AUDIENCE = "leafy-bank"
SECRET_ENV = "PARTY_AUTH_SECRET"

# Same demo default as the issuer, so a fresh clone works with no configuration and both
# services agree without it. Read lazily (defects.md 2026-06-30).
_DEV_SECRET = "leafy-bank-demo-party-auth-secret-do-not-use-in-production"

CUSTOMER = "CUSTOMER"


class AuthenticationError(Exception):
    """The token was presented and is not acceptable. Maps to HTTP 401."""


def require_authentication() -> bool:
    return os.getenv("REQUIRE_AUTHENTICATION", "false").strip().lower() in ("1", "true", "yes")


def _secret() -> str:
    return os.getenv(SECRET_ENV) or _DEV_SECRET


def bearer_token(header_value: Optional[str]) -> Optional[str]:
    """The token out of an `Authorization: Bearer …` header, or None if there isn't one."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def verify(token: str) -> dict:
    """Verify the signature, expiry, issuer and audience. Returns the claims.

    Every failure mode collapses to one message. A caller learning *why* its token was
    rejected — expired vs wrong signature vs wrong audience — is an oracle, and there is
    nothing it could do differently with the detail anyway.
    """
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        logger.info("rejected an authentication token: %s", type(exc).__name__)
        raise AuthenticationError("Authentication token is missing, expired or invalid.")


def resolve_identity(header_value: Optional[str], requested_customer_id: str) -> dict:
    """Turn the header into `{customer_ref, authentication}` for the payment saga.

    Returns the customer the payment must be initiated for, and the assertion stage 2 will
    grade. With no token and `REQUIRE_AUTHENTICATION` off, both come back unauthenticated:
    the body's `customerId` and `authentication: None`, which stage 2 records as
    `method: NONE` and a **SKIP** — never a PASS. That is the same honest reading it has
    always had for an absent assertion.
    """
    token = bearer_token(header_value)
    if token is None:
        if require_authentication():
            raise AuthenticationError("Authentication token is required.")
        return {"customer_ref": requested_customer_id, "authentication": None}

    claims = verify(token)
    caller_type = claims.get("callerType")
    subject = claims["sub"]

    if caller_type == CUSTOMER:
        if requested_customer_id != subject:
            # Not a 403-with-detail: the refusal is the same shape as any other bad token.
            logger.info("token subject %s tried to initiate for %s", subject, requested_customer_id)
            raise AuthenticationError("Authenticated party may not initiate for another customer.")
        customer_ref = subject
    else:
        # An operator or API caller acts FOR a customer. The requested id stands, and the
        # operator's own session is what gets recorded against the payment.
        customer_ref = requested_customer_id

    return {
        "customer_ref": customer_ref,
        # The same shape `AuthenticationAssertionBody` produces, so stage 2 does not care
        # which source it came from — but built from verified claims, not from a request.
        "authentication": {
            "method": claims.get("method"),
            "factorCount": claims.get("factorCount") or 0,
            "sessionRef": claims.get("sessionRef"),
            "authenticatedAt": claims.get("authenticatedAt"),
            "callerType": caller_type,
            # True when the session was raised by `Question/Evaluate` because stage 2's
            # step-up threshold demanded it. Recorded, not graded: `authentication_sufficient`
            # reads `method` and `factorCount`, so a forged `stepUp` would buy nothing.
            "stepUp": bool(claims.get("stepUp")),
        },
    }
