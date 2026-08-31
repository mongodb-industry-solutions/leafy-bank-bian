"""Pydantic request models for the BIAN PartyReferenceDataDirectory +
CurrentAccount service domains.

Field names use camelCase alias names (matching Mongo storage). The registry
handles BIAN documentation mapping; no wire translation is done at request time.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PartyApexStatusType = Literal["PROSPECT", "ACTIVE", "DORMANT", "SUSPENDED", "CLOSED"]
PartyTypeEnum = Literal[
    "INDIVIDUAL", "CORPORATE", "SME", "TRUST", "GOVERNMENT", "FINANCIAL_INSTITUTION"
]
CurrentAccountApexStatusType = Literal[
    "PENDING_ACTIVATION", "ACTIVE", "DORMANT", "FROZEN", "CLOSED", "CHARGED_OFF"
]
CurrentAccountTypeEnum = Literal[
    "CURRENT", "SAVINGS", "FIXED_DEPOSIT", "NOSTRO", "VOSTRO", "GL_ACCOUNT"
]


# ---------- PartyReferenceDataDirectory ----------
# Retrieve / CustomerKYCRecord Retrieve are GET (query params), no request model.

class PartyReferenceRequestRequest(BaseModel):
    status: Optional[PartyApexStatusType] = None
    segment: Optional[str] = None
    type: Optional[PartyTypeEnum] = None
    model_config = ConfigDict(extra="forbid")


# ---------- PartyAuthentication ----------

CallerTypeEnum = Literal["CUSTOMER", "OPERATOR", "API"]


class PartyAuthenticationEvaluateRequest(BaseModel):
    """`POST /PartyAuthentication/Evaluate` — BIAN SD 38917.

    `partyReference` is the `customers.customerId` for a CUSTOMER, and a staff identifier
    for an OPERATOR / API caller (who has no `customers` document). No credential field:
    Level 1 verifies that the party exists and is ACTIVE, not that it holds a secret. When
    `Password/Evaluate` lands, the credential goes on its own request model, not this one —
    a secret must never be optional on the request that also works without it.
    """

    partyReference: str = Field(min_length=1)
    callerType: CallerTypeEnum = "CUSTOMER"
    model_config = ConfigDict(extra="forbid")


class PartyAuthenticationQuestionEvaluateRequest(BaseModel):
    """`POST /PartyAuthentication/{partyauthenticationid}/Question/Evaluate` — SD 38917.

    The step-up factor stage 2's `authentication_sufficient` demands above a segment's
    threshold. No `partyReference`: who is stepping up comes from the bearer token, never
    from the body — a step-up that took the party's identity from the request would let a
    caller mint a two-factor assessment for anyone.
    """

    challengeResponse: str = Field(min_length=1, max_length=32)
    questionId: str = "otp"
    model_config = ConfigDict(extra="forbid")


# ---------- CurrentAccount ----------
# Retrieve / CurrentAccountBalanceRecord Retrieve are GET (query params), no request model.

class AccountInitiateRequest(BaseModel):
    customerId: str = Field(min_length=1)
    productId: Optional[str] = None
    type: CurrentAccountTypeEnum
    accountNumber: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    initialDeposit: float = Field(ge=0)
    model_config = ConfigDict(extra="forbid")


class AccountRequestRequest(BaseModel):
    customerId: Optional[str] = None
    status: Optional[CurrentAccountApexStatusType] = None
    type: Optional[CurrentAccountTypeEnum] = None
    model_config = ConfigDict(extra="forbid")


class AccountControlRequest(BaseModel):
    accountId: str = Field(min_length=1)
    controlAction: Literal["Close"]
    controlReason: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class AccountActivityRequestRequest(BaseModel):
    accountId: Optional[str] = None
    customerId: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.accountId) == bool(self.customerId):
            raise ValueError("Exactly one of accountId or customerId is required.")
        return self
