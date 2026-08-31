"""Static catalog describing the BIAN v14 API contract for the Leafy Bank demo.

This is the single source of truth for the "BIAN API" tab in the frontend
explorer modal. The catalog spans two backend services:

  - leafy-bank-backend-accounts (this service, :8080)
      * PartyAuthentication
      * PartyReferenceDataDirectory
      * CurrentAccount
  - leafy-bank-backend-transactions (:8001)
      * PaymentOrderInitiation

Conventions captured at the top level apply to every operation:
  - Initiate uses POST; Retrieve uses GET with the resource ID in the path.
  - Request bodies use BIAN PascalCase field names; unknown fields are
    rejected (extra="forbid" -> 422).
  - Money values are JSON numbers; currency is ISO-4217 (3 chars).
  - IDs are opaque prefixed strings (CUST-, ACC-, PAY-, TXN-).

Per-operation metadata:
  id                       - stable client-side key
  bianBehaviorQualifier    - optional BIAN sub-record (e.g. "CustomerKYCRecord")
  bianAction               - Retrieve | Request | Initiate | Control
  method                   - "GET" for Retrieve; "POST" for mutations (Initiate/Control) and Request
  path                     - PascalCase BIAN URL
  summary                  - one-line human description
  headers                  - list of header descriptors (e.g. Idempotency-Key)
  enums                    - field -> [allowed values]
  request                  - { required, notes, examples: [{label, value}] }
  response                 - { successCodes, envelopeKeys, example }
  errors                   - list of { code, case }
"""

API_CATALOG = {
    "version": "v14",
    "description": (
        "BIAN v14 contract layer for Leafy Bank. Verb-in-URL; Retrieve is GET, mutations are POST. "
        "Two services: accounts (:8080) and transactions (:8001)."
    ),
    "conventions": {
        "method": "POST",
        "verbInUrl": True,
        "extraFieldsRejected": True,
        "moneyEncoding": "json-number",
        "currencyStandard": "ISO-4217",
        "errorBody": {"detail": "string"},
        "idFormats": {
            "CustomerReference": "CUST-...",
            "CurrentAccountReference": "ACC-...",
            "PaymentOrderReference": "PAY-...",
            "CurrentAccountPaymentTransactionReference": "TXN-...-DEBIT|CREDIT",
        },
    },
    "statusCodes": [
        {"code": 200, "meaning": "Success (Retrieve / Request / Control / Initiate-replay)"},
        {"code": 201, "meaning": "Initiate created a new resource (some Initiates return 200; treat both as success)"},
        {"code": 400, "meaning": "Service-rule violation (debtor=creditor, currency mismatch, amount over limit, etc.)"},
        {"code": 404, "meaning": "Reference not found"},
        {"code": 422, "meaning": "Pydantic validation failed (missing field, bad enum, unknown field, type error)"},
        {"code": 500, "meaning": "Server bug / unhandled exception"},
    ],
    "services": [
        {
            "key": "accounts",
            "name": "leafy-bank-backend-accounts",
            "host": "http://localhost",
            "port": 8080,
            "serviceDomains": [
                {
                    "key": "PartyAuthentication",
                    "label": "Party Authentication",
                    "description": (
                        "Authenticates a party and returns a signed, expiring assessment token "
                        "(BIAN SD 38917, Sales and Service / Cross Channel). The transactions "
                        "service verifies this token and derives the payment's caller identity "
                        "from it rather than from the request body. LEVEL 1: no credential is "
                        "verified — selecting a persona IS the credential, and the assessment "
                        "says so via credentialVerified: false."
                    ),
                    "operations": [
                        {
                            "id": "evaluatePartyAuthentication",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Evaluate",
                            "method": "POST",
                            "path": "/PartyAuthentication/Evaluate",
                            "summary": "Authenticate a party; returns the assessment and a bearer token.",
                            "headers": [],
                            "enums": {"callerType": ["CUSTOMER", "OPERATOR", "API"]},
                            "request": {
                                "required": ["partyReference"],
                                "notes": (
                                    "partyReference is a customerId for CUSTOMER, and a staff "
                                    "identifier for OPERATOR/API (who have no customers document). "
                                    "An OPERATOR token may initiate a payment FOR a customer; a "
                                    "CUSTOMER token may only initiate for itself."
                                ),
                                "examples": [
                                    {"label": "Customer", "value": {
                                        "partyReference": "CUST-f88fb89e",
                                        "callerType": "CUSTOMER",
                                    }},
                                    {"label": "Back-office operator", "value": {
                                        "partyReference": "OPS-nadia",
                                        "callerType": "OPERATOR",
                                    }},
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": [
                                    "partyAuthenticationId", "assessment", "accessToken",
                                    "tokenType", "expiresAt",
                                ],
                                "example": {
                                    "partyAuthenticationId": "SESS-9F2C1A4B7D01",
                                    "assessment": {
                                        "partyReference": "CUST-f88fb89e",
                                        "callerType": "CUSTOMER",
                                        "method": "PASSWORD",
                                        "factorCount": 1,
                                        "credentialVerified": False,
                                    },
                                    "accessToken": "eyJhbGciOiJIUzI1NiIs…",
                                    "tokenType": "Bearer",
                                },
                            },
                            "errors": [
                                {"code": 401, "case": "Party does not exist, or is not ACTIVE — one refusal for both, so the endpoint cannot be used to probe which customer ids exist"},
                                {"code": 422, "case": "Validation error (missing partyReference, unknown callerType)"},
                            ],
                        },
                        {
                            "id": "retrievePartyAuthenticationQuestion",
                            "bianBehaviorQualifier": "Question",
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/PartyAuthentication/{partyauthenticationid}/Question/{questionid}/Retrieve",
                            "summary": "The step-up challenge for a live session. SIMULATED — returns the code itself.",
                            "headers": [
                                {"name": "Authorization",
                                 "notes": "Bearer token from POST /PartyAuthentication/Evaluate. Required — the "
                                          "token must name the session in the path."},
                            ],
                            "enums": {"questionId": ["otp"]},
                            "request": {
                                "required": [],
                                "notes": (
                                    "A real deployment would never return the code — it goes to a "
                                    "registered device out of band. deliveryChannel is "
                                    "ON_SCREEN_SIMULATION and simulated is true to say so."
                                ),
                                "examples": [],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": [
                                    "partyAuthenticationId", "questionId", "questionText",
                                    "challengeCode", "deliveryChannel", "simulated",
                                ],
                                "example": {
                                    "partyAuthenticationId": "SESS-9F2C1A4B7D01",
                                    "questionId": "otp",
                                    "challengeCode": "482067",
                                    "deliveryChannel": "ON_SCREEN_SIMULATION",
                                    "simulated": True,
                                },
                            },
                            "errors": [
                                {"code": 401, "case": "Missing/expired/invalid token, or a token naming a different session"},
                                {"code": 404, "case": "No such questionId on this assessment"},
                            ],
                        },
                        {
                            "id": "evaluatePartyAuthenticationQuestion",
                            "bianBehaviorQualifier": "Question",
                            "bianAction": "Evaluate",
                            "method": "POST",
                            "path": "/PartyAuthentication/{partyauthenticationid}/Question/Evaluate",
                            "summary": "Grade the step-up challenge; re-issues the session at OTP / 2 factors.",
                            "headers": [
                                {"name": "Authorization",
                                 "notes": "Bearer token from POST /PartyAuthentication/Evaluate. Required — a "
                                          "step-up strengthens an existing session and must name it."},
                            ],
                            "enums": {"questionId": ["otp"]},
                            "request": {
                                "required": ["challengeResponse"],
                                "notes": (
                                    "No partyReference: who is stepping up comes from the token, never "
                                    "the body. This is what makes the entitlement policy's step-up "
                                    "threshold satisfiable — above it, stage 2 refuses PASSWORD/1 with "
                                    "'step-up authentication required', and this answers that refusal. "
                                    "The code is an HMAC over the session ref, so a wrong code is "
                                    "genuinely refused; credentialVerified stays false because the code "
                                    "is delivered to the same screen that answers it."
                                ),
                                "examples": [
                                    {"label": "Step-up", "value": {
                                        "challengeResponse": "482067",
                                        "questionId": "otp",
                                    }},
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": [
                                    "partyAuthenticationId", "assessment", "accessToken",
                                    "tokenType", "expiresAt",
                                ],
                                "example": {
                                    "partyAuthenticationId": "SESS-42C2D9DFE8E9",
                                    "assessment": {
                                        "partyReference": "CUST-abc10001",
                                        "callerType": "CUSTOMER",
                                        "method": "OTP",
                                        "factorCount": 2,
                                        "stepUp": True,
                                        "credentialVerified": False,
                                        "simulated": True,
                                    },
                                    "accessToken": "eyJhbGciOiJIUzI1NiIs…",
                                    "tokenType": "Bearer",
                                },
                            },
                            "errors": [
                                {"code": 401, "case": "Bad token, a token naming a different session, or a wrong challenge code — one message for all three"},
                                {"code": 422, "case": "Validation error (missing challengeResponse)"},
                            ],
                        },
                    ],
                },
                {
                    "key": "PartyReferenceDataDirectory",
                    "label": "Party Reference Data Directory",
                    "description": "Customer master data and KYC.",
                    "operations": [
                        {
                            "id": "retrieveCustomer",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/PartyReferenceDataDirectory/{partyreferencedatadirectoryid}/Retrieve",
                            "summary": "Look up one customer by CustomerReference.",
                            "headers": [],
                            "enums": {},
                            "request": {
                                "required": ["CustomerReference"],
                                "notes": None,
                                "examples": [
                                    {
                                        "label": "by reference",
                                        "value": {"CustomerReference": "CUST-abc123"},
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CustomerReference", "PartyReferenceDataDirectoryEntryRecord"],
                                "example": {
                                    "CustomerReference": "CUST-abc123",
                                    "PartyReferenceDataDirectoryEntryRecord": {
                                        "CustomerReference": "CUST-abc123",
                                        "PartyApexStatus": "ACTIVE",
                                        "PartyType": "INDIVIDUAL",
                                        "CustomerSegmentType": "RETAIL",
                                        "PartyIdentification": {
                                            "PartyLegalName": "Jane Doe",
                                            "PartyDateOfBirth": "1985-04-12",
                                            "PartyContactRecord": {
                                                "PartyContactEmail": "jane@example.com",
                                                "PartyContactPhone": "+1-555-0100",
                                            },
                                        },
                                    },
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "CustomerReference not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                        {
                            "id": "requestCustomers",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Request",
                            "method": "POST",
                            "path": "/PartyReferenceDataDirectory/Request",
                            "summary": (
                                "List/query customers. All filters optional; empty body returns all. "
                                "Returns directory summaries (customerId, identification, status, "
                                "segment, type) — use /{id}/Retrieve for the full party record."
                            ),
                            "headers": [],
                            "enums": {
                                "PartyApexStatus": [
                                    "PROSPECT", "ACTIVE", "DORMANT", "SUSPENDED", "CLOSED",
                                ],
                                "PartyType": [
                                    "INDIVIDUAL", "CORPORATE", "SME", "TRUST",
                                    "GOVERNMENT", "FINANCIAL_INSTITUTION",
                                ],
                            },
                            "request": {
                                "required": [],
                                "notes": "All filters optional. Empty body returns all customers.",
                                "examples": [
                                    {
                                        "label": "all customers",
                                        "value": {},
                                    },
                                    {
                                        "label": "filtered",
                                        "value": {
                                            "PartyApexStatus": "ACTIVE",
                                            "CustomerSegmentType": "RETAIL",
                                            "PartyType": "INDIVIDUAL",
                                        },
                                    },
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["PartyReferenceDataDirectoryEntryRecord"],
                                "example": {
                                    "PartyReferenceDataDirectoryEntryRecord": [
                                        {
                                            "CustomerReference": "CUST-abc123",
                                            "PartyApexStatus": "ACTIVE",
                                            "PartyType": "INDIVIDUAL",
                                            "CustomerSegmentType": "RETAIL",
                                        },
                                        {
                                            "CustomerReference": "CUST-def456",
                                            "PartyApexStatus": "ACTIVE",
                                            "PartyType": "INDIVIDUAL",
                                            "CustomerSegmentType": "RETAIL",
                                        },
                                    ]
                                },
                            },
                            "errors": [
                                {"code": 422, "case": "Bad enum value or unknown field"},
                            ],
                        },
                        {
                            "id": "retrieveCustomerKYC",
                            "bianBehaviorQualifier": "CustomerKYCRecord",
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/PartyReferenceDataDirectory/{partyreferencedatadirectoryid}/CustomerKYCRecord/Retrieve",
                            "summary": "Retrieve the KYC record for one customer.",
                            "headers": [],
                            "enums": {},
                            "request": {
                                "required": ["CustomerReference"],
                                "notes": None,
                                "examples": [
                                    {
                                        "label": "by reference",
                                        "value": {"CustomerReference": "CUST-abc123"},
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CustomerReference", "CustomerKYCRecord"],
                                "example": {
                                    "CustomerReference": "CUST-abc123",
                                    "CustomerKYCRecord": {
                                        "KYCStatus": "VERIFIED",
                                        "KYCVerificationDate": "2024-08-15",
                                        "KYCDocumentType": "PASSPORT",
                                        "KYCDocumentReference": "P12345678",
                                    },
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "CustomerReference not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                    ],
                },
                {
                    "key": "CurrentAccount",
                    "label": "Current Account Fulfillment Arrangement",
                    "description": "Account opening, retrieval, listing, control, balance, and transaction history.",
                    "operations": [
                        {
                            "id": "initiateCurrentAccount",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Initiate",
                            "method": "POST",
                            "path": "/CurrentAccount/Initiate",
                            "summary": "Open a new current account for an existing customer.",
                            "headers": [],
                            "enums": {
                                "CurrentAccountType": [
                                    "CURRENT", "SAVINGS", "FIXED_DEPOSIT",
                                    "NOSTRO", "VOSTRO", "GL_ACCOUNT",
                                ],
                            },
                            "request": {
                                "required": [
                                    "CustomerReference",
                                    "CurrentAccountType",
                                    "CurrentAccountNumber",
                                    "CurrentAccountCurrencyCode",
                                    "InitialDepositAmount",
                                ],
                                "notes": (
                                    "ProductReference optional. "
                                    "CurrentAccountCurrencyCode must be 3-char ISO-4217. "
                                    "InitialDepositAmount must be >= 0."
                                ),
                                "examples": [
                                    {
                                        "label": "open checking",
                                        "value": {
                                            "CustomerReference": "CUST-abc123",
                                            "ProductReference": "PROD-CHK-001",
                                            "CurrentAccountType": "CURRENT",
                                            "CurrentAccountNumber": "100200300",
                                            "CurrentAccountCurrencyCode": "USD",
                                            "InitialDepositAmount": 500.0,
                                        },
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CurrentAccountReference", "CurrentAccountFulfillmentArrangementRecord"],
                                "example": {
                                    "CurrentAccountReference": "ACC-xyz789",
                                    "CurrentAccountFulfillmentArrangementRecord": {
                                        "CurrentAccountReference": "ACC-xyz789",
                                        "CustomerReference": "CUST-abc123",
                                        "CurrentAccountNumber": "100200300",
                                        "CurrentAccountType": "CURRENT",
                                        "CurrentAccountCurrencyCode": "USD",
                                        "CurrentAccountApexStatus": "ACTIVE",
                                        "CurrentAccountBalanceRecord": {
                                            "CurrentAccountAvailableBalance": 500.0,
                                            "CurrentAccountLedgerBalance": 500.0,
                                        },
                                    },
                                },
                            },
                            "errors": [
                                {"code": 400, "case": "Duplicate CurrentAccountNumber"},
                                {"code": 400, "case": "Customer not active"},
                                {"code": 404, "case": "CustomerReference not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                        {
                            "id": "retrieveCurrentAccount",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/CurrentAccount/{currentaccountid}/Retrieve",
                            "summary": "Look up one account by CurrentAccountReference or CurrentAccountNumber.",
                            "headers": [],
                            "enums": {},
                            "request": {
                                "required": [],
                                "notes": (
                                    "At least one of CurrentAccountReference or CurrentAccountNumber required. "
                                    "Returns 422 if neither field is present."
                                ),
                                "examples": [
                                    {
                                        "label": "by account reference",
                                        "value": {"CurrentAccountReference": "ACC-xyz789"},
                                    },
                                    {
                                        "label": "by account number",
                                        "value": {"CurrentAccountNumber": "100200300"},
                                    },
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CurrentAccountReference", "CurrentAccountFulfillmentArrangementRecord"],
                                "example": {
                                    "CurrentAccountReference": "ACC-xyz789",
                                    "CurrentAccountFulfillmentArrangementRecord": {
                                        "CurrentAccountReference": "ACC-xyz789",
                                        "CustomerReference": "CUST-abc123",
                                        "CurrentAccountNumber": "100200300",
                                        "CurrentAccountType": "CURRENT",
                                        "CurrentAccountCurrencyCode": "USD",
                                        "CurrentAccountApexStatus": "ACTIVE",
                                        "CurrentAccountBalanceRecord": {
                                            "CurrentAccountAvailableBalance": 1234.56,
                                            "CurrentAccountLedgerBalance": 1234.56,
                                        },
                                    },
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "Account not found"},
                                {"code": 422, "case": "Neither CurrentAccountReference nor CurrentAccountNumber provided"},
                            ],
                        },
                        {
                            "id": "requestCurrentAccounts",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Request",
                            "method": "POST",
                            "path": "/CurrentAccount/Request",
                            "summary": "List/query accounts. All filters optional.",
                            "headers": [],
                            "enums": {
                                "CurrentAccountApexStatus": [
                                    "PENDING_ACTIVATION", "ACTIVE", "DORMANT",
                                    "FROZEN", "CLOSED", "CHARGED_OFF",
                                ],
                                "CurrentAccountType": [
                                    "CURRENT", "SAVINGS", "FIXED_DEPOSIT",
                                    "NOSTRO", "VOSTRO", "GL_ACCOUNT",
                                ],
                            },
                            "request": {
                                "required": [],
                                "notes": "All filters optional. Empty body returns all accounts.",
                                "examples": [
                                    {
                                        "label": "all accounts for customer",
                                        "value": {"CustomerReference": "CUST-abc123"},
                                    },
                                    {
                                        "label": "active checking accounts for customer",
                                        "value": {
                                            "CustomerReference": "CUST-abc123",
                                            "CurrentAccountApexStatus": "ACTIVE",
                                            "CurrentAccountType": "CURRENT",
                                        },
                                    },
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CurrentAccountFulfillmentArrangementRecord"],
                                "example": {
                                    "CurrentAccountFulfillmentArrangementRecord": [
                                        {
                                            "CurrentAccountReference": "ACC-xyz789",
                                            "CurrentAccountApexStatus": "ACTIVE",
                                            "CurrentAccountType": "CURRENT",
                                        },
                                        {
                                            "CurrentAccountReference": "ACC-pqr456",
                                            "CurrentAccountApexStatus": "ACTIVE",
                                            "CurrentAccountType": "SAVINGS",
                                        },
                                    ]
                                },
                            },
                            "errors": [
                                {"code": 422, "case": "Bad enum value or unknown field"},
                            ],
                        },
                        {
                            "id": "controlCurrentAccount",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Control",
                            "method": "POST",
                            "path": "/CurrentAccount/Control",
                            "summary": "State-change action on an account. Phase 1 supports Close only.",
                            "headers": [],
                            "enums": {
                                "ControlActionType": ["Close"],
                            },
                            "request": {
                                "required": ["CurrentAccountReference", "ControlActionType"],
                                "notes": "ControlActionReason optional. Phase 1: only ControlActionType=\"Close\" is accepted.",
                                "examples": [
                                    {
                                        "label": "close account",
                                        "value": {
                                            "CurrentAccountReference": "ACC-xyz789",
                                            "ControlActionType": "Close",
                                            "ControlActionReason": "Customer requested closure",
                                        },
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CurrentAccountReference", "ControlActionType", "CurrentAccountFulfillmentArrangementRecord"],
                                "example": {
                                    "CurrentAccountReference": "ACC-xyz789",
                                    "ControlActionType": "Close",
                                    "CurrentAccountFulfillmentArrangementRecord": {
                                        "CurrentAccountReference": "ACC-xyz789",
                                        "CurrentAccountApexStatus": "CLOSED",
                                    },
                                },
                            },
                            "errors": [
                                {"code": 400, "case": "Account already closed"},
                                {"code": 400, "case": "Account has non-zero balance"},
                                {"code": 404, "case": "Account not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                        {
                            "id": "retrieveCurrentAccountBalance",
                            "bianBehaviorQualifier": "CurrentAccountBalanceRecord",
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/CurrentAccount/{currentaccountid}/CurrentAccountBalanceRecord/Retrieve",
                            "summary": "Get the current balance record for one account.",
                            "headers": [],
                            "enums": {},
                            "request": {
                                "required": ["CurrentAccountReference"],
                                "notes": None,
                                "examples": [
                                    {
                                        "label": "by account reference",
                                        "value": {"CurrentAccountReference": "ACC-xyz789"},
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": [
                                    "CurrentAccountReference",
                                    "CurrentAccountBalanceRecord",
                                    "CurrentAccountCurrencyCode",
                                ],
                                "example": {
                                    "CurrentAccountReference": "ACC-xyz789",
                                    "CurrentAccountBalanceRecord": {
                                        "CurrentAccountAvailableBalance": 1234.56,
                                        "CurrentAccountLedgerBalance": 1234.56,
                                        "CurrentAccountBalanceAsOfDateTime": "2026-04-28T10:15:30Z",
                                    },
                                    "CurrentAccountCurrencyCode": "USD",
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "Account not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                        {
                            "id": "requestCurrentAccountTransactions",
                            "bianBehaviorQualifier": "CurrentAccountTransaction",
                            "bianAction": "Request",
                            "method": "POST",
                            "path": "/CurrentAccount/CurrentAccountTransaction/Request",
                            "summary": "List recent ledger legs for an account (written by the transactions service).",
                            "headers": [],
                            "enums": {
                                "TransactionDirection": ["DEBIT", "CREDIT"],
                            },
                            "request": {
                                "required": ["CurrentAccountReference"],
                                "notes": "Limit optional. Default 20, range 1..100.",
                                "examples": [
                                    {
                                        "label": "last 20 transactions",
                                        "value": {"CurrentAccountReference": "ACC-xyz789", "Limit": 20},
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": ["CurrentAccountReference", "CurrentAccountPaymentTransactionRecord"],
                                "example": {
                                    "CurrentAccountReference": "ACC-xyz789",
                                    "CurrentAccountPaymentTransactionRecord": [
                                        {
                                            "CurrentAccountPaymentTransactionReference": "TXN-PAY-abc-DEBIT",
                                            "PaymentOrderReference": "PAY-abc",
                                            "TransactionAmount": 100.0,
                                            "TransactionCurrencyCode": "USD",
                                            "TransactionDirection": "DEBIT",
                                            "TransactionPostingDate": "2026-04-28T10:15:30Z",
                                            "CurrentAccountReference": "ACC-xyz789",
                                            "CounterpartyAccountReference": "ACC-pqr456",
                                        }
                                    ],
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "Account not found"},
                                {"code": 422, "case": "Limit out of range or invalid type"},
                            ],
                        },
                    ],
                },
            ],
        },
        {
            "key": "transactions",
            "name": "leafy-bank-backend-transactions",
            "host": "http://localhost",
            "port": 8001,
            "serviceDomains": [
                {
                    "key": "PaymentOrderInitiation",
                    "label": "Payment Order Initiation",
                    "description": "Submit and retrieve payment orders. Multi-document ACID transactions.",
                    "operations": [
                        {
                            "id": "initiatePaymentOrder",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Initiate",
                            "method": "POST",
                            "path": "/PaymentOrderInitiation/Initiate",
                            "summary": (
                                "Submit a new payment order. Multi-document ACID: atomically writes "
                                "the payment, two ledger legs, balance updates, and one notification."
                            ),
                            "headers": [
                                {
                                    "name": "Idempotency-Key",
                                    "required": False,
                                    "notes": (
                                        "Recommended client-generated UUID. Maps to idempotencyKey "
                                        "(NOT endToEndId, which is the server-derived ISO 20022 "
                                        "EndToEndIdentification). Replays with the same key return the "
                                        "original response without re-running the transaction."
                                    ),
                                },
                                {
                                    "name": "Authorization",
                                    "required": False,
                                    "notes": (
                                        "Bearer token from POST /PartyAuthentication/Evaluate. When "
                                        "present it OVERRIDES the body: a CUSTOMER token must match "
                                        "the body's customerId or the request is 401, while an "
                                        "OPERATOR token may initiate for any customer. When absent, "
                                        "the body's customerId is used and stage 2 records the "
                                        "authentication check as SKIP. Set REQUIRE_AUTHENTICATION=true "
                                        "to make the token mandatory (401 without one)."
                                    ),
                                },
                            ],
                            "enums": {
                                "PaymentType": [
                                    "CREDIT_TRANSFER", "DIRECT_DEBIT", "CARD_PAYMENT",
                                    "RTP", "STANDING_ORDER",
                                ],
                                "PaymentRailType": ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"],
                            },
                            "request": {
                                "required": [
                                    "CustomerReference",
                                    "PaymentType",
                                    "PaymentRailType",
                                    "PaymentDebtorRecord",
                                    "PaymentCreditorRecord",
                                    "PaymentInstructedAmount",
                                    "PaymentInstructedCurrencyCode",
                                ],
                                "notes": (
                                    "Phase 1 settles INTERNAL only; a WIRE to an external "
                                    "beneficiary is captured and stops at SUBMITTED. "
                                    "PaymentInstructedAmount must be > 0. "
                                    "PaymentInstructedCurrencyCode must be 3-char ISO-4217. "
                                    "CreditorAccountReference is null for an external beneficiary, "
                                    "which then requires CreditorAccountNumber + CreditorPartyName "
                                    "(and a BIC on the WIRE rail). "
                                    "PaymentRequestedExecutionDate defaults to today. "
                                    "PaymentRemittanceRecord optional."
                                ),
                                "examples": [
                                    {
                                        "label": "internal transfer",
                                        "value": {
                                            "CustomerReference": "CUST-abc123",
                                            "PaymentType": "CREDIT_TRANSFER",
                                            "PaymentRailType": "INTERNAL",
                                            "PaymentDebtorRecord": {
                                                "DebtorAccountReference": "ACC-xyz789"
                                            },
                                            "PaymentCreditorRecord": {
                                                "CreditorAccountReference": "ACC-pqr456"
                                            },
                                            "PaymentInstructedAmount": 100.0,
                                            "PaymentInstructedCurrencyCode": "USD",
                                            "PaymentRequestedExecutionDate": "2026-08-28",
                                            "PaymentRemittanceRecord": {
                                                "RemittanceUnstructuredInformationText": "Lunch payback"
                                            },
                                        },
                                    },
                                    {
                                        "label": "domestic wire to an external beneficiary",
                                        "value": {
                                            "CustomerReference": "CUST-abc123",
                                            "PaymentType": "CREDIT_TRANSFER",
                                            "PaymentRailType": "WIRE",
                                            "PaymentDebtorRecord": {
                                                "DebtorAccountReference": "ACC-xyz789"
                                            },
                                            "PaymentCreditorRecord": {
                                                "CreditorAccountNumber": "9876543210",
                                                "CreditorPartyName": "Acme Corp",
                                                "CreditorBankIdentifierCode": "CHASUS33",
                                                "CreditorBankName": "JPMorgan Chase",
                                                "CreditorBankCountryCode": "US",
                                            },
                                            "PaymentInstructedAmount": 2500.0,
                                            "PaymentInstructedCurrencyCode": "USD",
                                            "PaymentRequestedExecutionDate": "2026-08-28",
                                            "PaymentWireInitiationRecord": {
                                                "WireTransferType": "DOMESTIC"
                                            },
                                        },
                                    },
                                ],
                            },
                            "response": {
                                "successCodes": [200, 201],
                                "envelopeKeys": [
                                    "PaymentOrderReference",
                                    "PaymentApexStatus",
                                    "PaymentOrderRecord",
                                ],
                                "example": {
                                    "PaymentOrderReference": "PAY-abc",
                                    "PaymentApexStatus": "COMPLETED",
                                    "PaymentOrderRecord": {
                                        "PaymentOrderReference": "PAY-abc",
                                        "CustomerReference": "CUST-abc123",
                                        "PaymentType": "CREDIT_TRANSFER",
                                        "PaymentRailType": "INTERNAL",
                                        "PaymentApexStatus": "COMPLETED",
                                        "PaymentDebtorRecord": {"DebtorAccountReference": "ACC-xyz789"},
                                        "PaymentCreditorRecord": {"CreditorAccountReference": "ACC-pqr456"},
                                        "PaymentInstructedAmount": 100.0,
                                        "PaymentInstructedCurrencyCode": "USD",
                                        "PaymentInitiationDateTime": "2026-04-28T10:15:30Z",
                                        "PaymentRemittanceRecord": {
                                            "RemittanceUnstructuredInformationText": "Lunch payback"
                                        },
                                    },
                                },
                            },
                            "errors": [
                                {"code": 400, "case": "Debtor and creditor are the same account"},
                                {"code": 400, "case": "Debtor account not active"},
                                {"code": 400, "case": "Currency mismatch between request and account(s)"},
                                {"code": 400, "case": "Amount over PAYMENT_LIMIT_USD (default 1000000) — a malformed-input bound"},
                                {"code": 400, "case": "Amount over the customer segment's per-payment entitlement (stage 2)"},
                                {"code": 400, "case": "Insufficient funds"},
                                {"code": 404, "case": "Debtor or creditor account not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                            "notesFooter": (
                                "Idempotency replay: a request with a previously-seen "
                                "Idempotency-Key returns the original 200 response and does NOT "
                                "re-execute the transaction."
                            ),
                        },
                        {
                            "id": "retrievePaymentOrder",
                            "bianBehaviorQualifier": None,
                            "bianAction": "Retrieve",
                            "method": "GET",
                            "path": "/PaymentOrderInitiation/{paymentorderinitiationid}/Retrieve",
                            "summary": "Look up a single payment order plus its ledger legs.",
                            "headers": [],
                            "enums": {},
                            "request": {
                                "required": ["paymentorderinitiationid"],
                                "notes": "paymentorderinitiationid is the PaymentOrderReference (e.g. PAY-abc) passed as a URL path parameter.",
                                "examples": [
                                    {
                                        "label": "by payment reference",
                                        "value": {"paymentorderinitiationid": "PAY-abc"},
                                    }
                                ],
                            },
                            "response": {
                                "successCodes": [200],
                                "envelopeKeys": [
                                    "PaymentOrderReference",
                                    "PaymentOrderRecord",
                                    "CurrentAccountPaymentTransactionRecord",
                                ],
                                "example": {
                                    "PaymentOrderReference": "PAY-abc",
                                    "PaymentOrderRecord": {
                                        "PaymentOrderReference": "PAY-abc",
                                        "PaymentApexStatus": "COMPLETED",
                                        "PaymentDebtorRecord": {"DebtorAccountReference": "ACC-xyz789"},
                                        "PaymentCreditorRecord": {"CreditorAccountReference": "ACC-pqr456"},
                                        "PaymentInstructedAmount": 100.0,
                                        "PaymentInstructedCurrencyCode": "USD",
                                    },
                                    "CurrentAccountPaymentTransactionRecord": [
                                        {
                                            "CurrentAccountPaymentTransactionReference": "TXN-PAY-abc-DEBIT",
                                            "TransactionDirection": "DEBIT",
                                            "CurrentAccountReference": "ACC-xyz789",
                                            "TransactionAmount": 100.0,
                                            "TransactionCurrencyCode": "USD",
                                        },
                                        {
                                            "CurrentAccountPaymentTransactionReference": "TXN-PAY-abc-CREDIT",
                                            "TransactionDirection": "CREDIT",
                                            "CurrentAccountReference": "ACC-pqr456",
                                            "TransactionAmount": 100.0,
                                            "TransactionCurrencyCode": "USD",
                                        },
                                    ],
                                },
                            },
                            "errors": [
                                {"code": 404, "case": "PaymentOrderReference not found"},
                                {"code": 422, "case": "Validation error"},
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}
