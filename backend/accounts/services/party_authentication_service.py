"""BIAN PartyAuthentication (SD 38917) — the Evaluate behaviour.

`POST /PartyAuthentication/Evaluate` is BIAN's own operation name for "authenticate this
party and return an assessment", and `POST /PartyAuthentication/{id}/Question/Evaluate` is
its own name for grading a challenge-response factor — both verified against the local KG
(`kg PartyAuthentication -s bian`). Siblings the KG lists and this service does not
implement: `Password/Evaluate` (credential verification, still gated — the canonical model
has nowhere to store a secret), `Biometric/Evaluate`, `Device/Evaluate`, and
`Grant` / `Exchange`, which need the assessment to be persisted. See
`shared/party_authentication.py` for why it is not.

## Why Question/Evaluate exists now

Stage 2's `authentication_sufficient` refuses a single weak factor above the segment's
step-up threshold — COMMERCIAL at 10,000. Level 1 mints `PASSWORD`/1 and nothing else, so
the flagship $25,000 corporate payment was **unreachable through the live login path**: the
refusal was correct and there was no way to satisfy it. The test suite missed it because
its fixture asserts `OTP`/2 by hand, which is a capability the live system could not
produce once the hardcoded frontend assertions were deleted.

This closes that loop the way the policy table intended — the step-up happens *because the
entitlement engine demanded it*, which is the demo beat, rather than by relaxing the
threshold, which would delete it.

What Evaluate actually verifies (Level 1): that the party exists in `customers` and is
`ACTIVE`. No secret is checked. That is a real server-side gate — a fabricated persona
cannot obtain a token — and it is not authentication. The distinction is recorded on the
assessment itself as `credentialVerified: false`.
"""

from __future__ import annotations

import hmac
import logging

from database.connection import MongoDBConnection
from shared import party_authentication as party_auth

logger = logging.getLogger(__name__)

# One click on a persona card. Single-factor, knowledge-ish, and stage 2 grades it against
# the amount: below the segment's step-up threshold it passes, above it the payment is
# refused. Both halves of that are live; the factor being graded is the simulated part.
_LEVEL_1_METHOD = "PASSWORD"
_LEVEL_1_FACTOR_COUNT = 1

# The step-up factor. `OTP` is in `entitlement_policy._STRONG_METHODS` and the count is 2,
# so either half of `authentication_sufficient` would carry it — deliberate: the assessment
# describes what was collected, and it is not the policy's job to be satisfied by exactly
# one of the two clauses. Both names come from the `method` enum on
# `AuthenticationAssertionBody`; neither is invented here.
_STEP_UP_METHOD = "OTP"
_STEP_UP_FACTOR_COUNT = 2

# The single question this service asks. BIAN addresses a question by id under an
# assessment (`Question/{questionid}/Retrieve`); there is exactly one kind here.
STEP_UP_QUESTION_ID = "otp"


class PartyAuthenticationService:
    def __init__(self, connection: MongoDBConnection, db_name: str):
        self.customers = connection.get_database(db_name)["customers"]

    def evaluate(self, *, party_reference: str, caller_type: str) -> dict:
        """Authenticate a party and return `{assessment, token, expiresAt}`.

        Raises `ValueError` for a party that cannot be authenticated; the router maps it to
        401. A caller must never be able to tell "no such customer" from "customer is
        closed" — both are the same refusal here, for the same reason a login form does not
        say which half of the pair was wrong.
        """
        if caller_type == party_auth.CUSTOMER:
            customer = self.customers.find_one(
                {"customerId": party_reference},
                {"customerId": 1, "status": 1, "type": 1, "segment": 1},
            )
            if not customer or customer.get("status") != "ACTIVE":
                logger.info("PartyAuthentication/Evaluate refused for %s", party_reference)
                raise ValueError("Party could not be authenticated.")
        # An OPERATOR or API caller is not a bank customer and has no `customers` document
        # — the back-office personas are staff. There is nothing to look up, and that is
        # exactly why their token must NOT be read as a customer's: the transactions
        # service honours the requested customerId for these callers and records the
        # operator alongside it. Which customer they are acting for is a property of the
        # payment, not of the session, so it is not a claim.

        issued = party_auth.issue(
            subject=party_reference,
            caller_type=caller_type,
            method=_LEVEL_1_METHOD,
            factor_count=_LEVEL_1_FACTOR_COUNT,
        )
        logger.info(
            "PartyAuthentication/Evaluate issued %s for %s (%s)",
            issued["assessment"]["sessionRef"], party_reference, caller_type,
        )
        return issued

    def retrieve_question(self, *, token: str, party_authentication_id: str) -> dict:
        """The challenge for a live session — `Question/{questionid}/Retrieve`.

        Returns the code, which in a real bank it would never do: the code would go to a
        registered device out of band. The response says so in `deliveryChannel` and
        `simulated` so a screenshot of it cannot be mistaken for a real OTP flow.
        """
        claims = self._claims_for_session(token, party_authentication_id)
        session_ref = claims["sessionRef"]

        return {
            "partyAuthenticationId": session_ref,
            "questionId": STEP_UP_QUESTION_ID,
            "questionText": "Enter the one-time code sent to your registered device.",
            "challengeCode": party_auth.challenge_code(session_ref, STEP_UP_QUESTION_ID),
            "deliveryChannel": "ON_SCREEN_SIMULATION",
            "simulated": True,
        }

    def evaluate_question(self, *, token: str, party_authentication_id: str,
                          challenge_response: str) -> dict:
        """Grade a challenge answer and, on success, re-issue the session at two factors.

        The existing token is required and must name this very session: step-up strengthens
        a session that already exists. Without that anchor a caller could mint an `OTP`/2
        assessment for any party it liked, which would make the strong route weaker than
        the weak one.

        Raises `ValueError` (→ 401) for a bad token, a session mismatch, or a wrong code.
        A wrong code is a real refusal — see `challenge_code` for why that matters.
        """
        claims = self._claims_for_session(token, party_authentication_id)
        session_ref = claims["sessionRef"]
        expected = party_auth.challenge_code(session_ref, STEP_UP_QUESTION_ID)

        # compare_digest, not `==`: the comparison is against an HMAC-derived secret, and a
        # timing-distinguishable check on one is a bad habit to leave in a reference demo.
        if not hmac.compare_digest(str(challenge_response).strip(), expected):
            logger.info("Question/Evaluate refused for session %s", session_ref)
            raise ValueError("Challenge response is incorrect.")

        # A NEW session ref, not a mutation of the old one: the assessment is immutable and
        # is not persisted, so "upgrading" it means issuing the stronger one. The payment
        # records whichever ref it was actually initiated under.
        issued = party_auth.issue(
            subject=claims["sub"],
            caller_type=claims["callerType"],
            method=_STEP_UP_METHOD,
            factor_count=_STEP_UP_FACTOR_COUNT,
            step_up=True,
        )
        logger.info(
            "Question/Evaluate stepped %s up to %s (%s, %d factors) for %s",
            session_ref, issued["assessment"]["sessionRef"],
            _STEP_UP_METHOD, _STEP_UP_FACTOR_COUNT, claims["sub"],
        )
        return issued

    @staticmethod
    def _claims_for_session(token: str, party_authentication_id: str) -> dict:
        """Verified claims, confirmed to belong to the session named in the path.

        One message for a bad token and for the wrong session, for the same reason
        `evaluate` gives one message for "no such party" and "not active".
        """
        claims = party_auth.verify(token)          # raises ValueError
        if claims.get("sessionRef") != party_authentication_id:
            logger.info(
                "step-up on session %s presented a token for %s",
                party_authentication_id, claims.get("sessionRef"),
            )
            raise ValueError("Authentication token is missing, expired or invalid.")
        return claims
