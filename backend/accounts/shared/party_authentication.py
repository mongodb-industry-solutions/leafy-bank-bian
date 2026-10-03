"""The PartyAuthentication token contract — issuing side.

BIAN PartyAuthentication (SD 38917, Sales and Service / Cross Channel, control record
`PartyAuthenticationAssessment`). This service domain lives on **accounts** because
accounts owns `customers`, and authentication is Party-domain work, not payments — which
is what `transactions/contexts/party_authentication/authenticate.py` has said in its
docstring since stage 2 was built.

## What this is, and what it deliberately is not

**It is** a server-issued, signed, expiring assertion of identity. Before it, `customerId`
was a plain body field a browser could type anything into, and the payments hub's ownership
check compared a client-supplied string against the account it named — circular.

**It is not** credential verification. Nothing here checks a secret: selecting a persona
IS the credential (Level 1). What it does check is that the party exists and is `ACTIVE`,
so a persona that is not a real, live customer cannot obtain a token at all. Verifying a
password, and the OTP step-up that stage 2's `authentication_sufficient` is already built
to demand, is Level 2 — and it needs somewhere to store a credential, which the canonical
model does not have (`bian.py grep password|credential|token` → **0 matches** across all 22
collections). That makes it Doina's call, the same shape as the entitlement-limits question.

So `method` is `PASSWORD` and `factorCount` is 1 because that is the honest description of
one click. Stage 2 grades it: under the segment's step-up threshold it passes, above it the
payment is refused. That refusal is real today; the factor it is refusing is not.

## The contract with the transactions service

`transactions/shared/party_authentication_token.py` verifies what this module signs. The
two sides share **configuration, not code** — the algorithm, the issuer/audience strings,
the claim names and the secret's env var are declared in both files and named as a pair,
rather than one importing the other or a module being mirrored byte-for-byte. Mirroring a
module across two Poetry projects is how `bian-alias-map.json` drift happens (umbrella
defects.md); a library call plus four constants cannot silently diverge in behaviour, only
in configuration, which fails loudly as a signature or audience error.

⚠️ `HS256` is a shared secret, so **any holder of `PARTY_AUTH_SECRET` can mint a token for
any customer.** Both services must read the same value. That is fine for a demo and is not
a production posture: there is no TLS story here and CORS is `*`. Asymmetric signing (RS256,
accounts holds the private key) is the upgrade, and it is a one-line algorithm change.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt

logger = logging.getLogger(__name__)

# Shared with transactions/shared/party_authentication_token.py — change both together.
ALGORITHM = "HS256"
ISSUER = "leafy-bank-accounts"
AUDIENCE = "leafy-bank"
SECRET_ENV = "PARTY_AUTH_SECRET"
TTL_ENV = "PARTY_AUTH_TOKEN_TTL_SECONDS"

# A demo default so a fresh clone works with no configuration, and so both services agree
# without one. Read LAZILY, never at module scope — a module-level `os.getenv` runs before
# `load_dotenv` and silently takes the default (defects.md 2026-06-30).
_DEV_SECRET = "leafy-bank-demo-party-auth-secret-do-not-use-in-production"
_DEFAULT_TTL_SECONDS = 3600

# `callerType` — who is holding the token. This is the distinction Doina's stage-2
# requirement 1 asks for ("the authenticated customer, corporate user, or API") and that
# nothing modelled before now.
CUSTOMER = "CUSTOMER"
OPERATOR = "OPERATOR"
API = "API"

CALLER_TYPES = (CUSTOMER, OPERATOR, API)


def _secret() -> str:
    secret = os.getenv(SECRET_ENV)
    if not secret:
        logger.warning(
            "%s is not set — using the built-in demo secret. Any holder of it can mint a "
            "token for any customer. Set it (same value in the transactions service) "
            "before any deployment that matters.", SECRET_ENV,
        )
        return _DEV_SECRET
    return secret


def _ttl_seconds() -> int:
    raw = os.getenv(TTL_ENV)
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %ds", TTL_ENV, raw, _DEFAULT_TTL_SECONDS)
        return _DEFAULT_TTL_SECONDS


def verify(token: str) -> dict:
    """Verify a token this module signed, and return its claims.

    Accounts verifies its own tokens for one reason only: `Question/Evaluate` must be
    anchored to an existing session. A step-up that minted a two-factor assessment for any
    `partyReference` a caller named would be strictly weaker than the Level 1 route it
    claims to strengthen.

    This is not the mirror of `transactions/shared/party_authentication_token.py`. That
    module is a *consumer* — it also decides `REQUIRE_AUTHENTICATION`, resolves CUSTOMER vs
    OPERATOR against a requested `customerId`, and shapes the stage-2 assertion. None of
    that belongs here. Both call `jwt.decode` with the same four constants declared above,
    which is configuration, not duplicated logic.

    Raises `ValueError` — the caller (a router) maps it to 401.
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
        logger.info("rejected a token at step-up: %s", type(exc).__name__)
        raise ValueError("Authentication token is missing, expired or invalid.")


def challenge_code(session_ref: str, question_id: str) -> str:
    """The six-digit code for one (session, question) pair. Deterministic, not stored.

    HMAC over the session ref keyed by the signing secret. Two consequences, both wanted:

      * the server can verify the answer with **no** persisted challenge — the canonical
        model has nowhere to store one (`bian.py grep password|credential|token` → 0
        matches), so a `Question/Evaluate` that had to remember what it asked would need a
        new collection in the shared DB, and therefore Doina.
      * a wrong code is genuinely refused, so a *failed* step-up is demonstrable. A
        challenge that accepts any input cannot show that, and a demo of a control that
        never says no is not a demo of a control.

    ⚠️ It is still a **simulation**, because the code is delivered to the same screen that
    answers it — there is no out-of-band channel here. That is why the assessment keeps
    `credentialVerified: false` and carries `simulated: true`. What is real: the code is
    bound to one session and cannot be replayed onto another.
    """
    digest = hmac.new(
        _secret().encode(), f"{session_ref}:{question_id}".encode(), hashlib.sha256
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def issue(*, subject: str, caller_type: str, method: str, factor_count: int,
          step_up: bool = False, now: datetime | None = None) -> dict:
    """Mint a `PartyAuthenticationAssessment` and return it with its signed token.

    Returns the assessment (what the channel may display and what stage 2 will grade) and
    the token (what the caller sends back). **The assessment is not persisted** — the token
    carries it, and the durable audit record is `payments.authentication{}`, written by
    stage 2 on the payment the assertion was actually used for. A
    `partyAuthenticationAssessments` collection would be needed for revocation or a
    session list; neither is a Level 1 requirement, and adding a collection to the shared
    `leafy_bank_bian` for a demo needs a better reason than symmetry.
    """
    if caller_type not in CALLER_TYPES:
        raise ValueError(f"unknown callerType {caller_type!r}")

    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_ttl_seconds())
    session_ref = f"SESS-{uuid.uuid4().hex[:12].upper()}"

    claims = {
        "sub": subject,
        "callerType": caller_type,
        "method": method,
        "factorCount": factor_count,
        "stepUp": step_up,
        "sessionRef": session_ref,
        "authenticatedAt": now.isoformat(),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }

    return {
        "assessment": {
            "partyAuthenticationId": session_ref,
            "partyReference": subject,
            "callerType": caller_type,
            "method": method,
            "factorCount": factor_count,
            "sessionRef": session_ref,
            "authenticatedAt": now,
            "expiresAt": expires,
            # Says out loud what was and was not verified, so no consumer has to infer it.
            "credentialVerified": False,
            # True when a second factor was collected via `Question/Evaluate`. The channel
            # displays it as SIMULATED; stage 2 grades `method`/`factorCount`, not this.
            "stepUp": step_up,
            "simulated": True,
            "assessedBy": ISSUER,
        },
        "token": jwt.encode(claims, _secret(), algorithm=ALGORITHM),
        "expiresAt": expires,
    }
