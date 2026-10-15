# Leafy Bank - Core Banking

![MongoDB](https://img.shields.io/badge/MongoDB-%234ea94b.svg?style=for-the-badge&logo=mongodb&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)
![Next JS](https://img.shields.io/badge/Next-black?style=for-the-badge&logo=next.js&logoColor=white)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![BIAN](https://img.shields.io/badge/BIAN-v14-orange?style=for-the-badge)

**Leafy Bank is a BIAN v14-aligned core-banking reference**, showcasing the integration of MongoDB's powerful features tailored specifically for [Financial Services](https://www.mongodb.com/solutions/industries/financial-services). It's built as four FastAPI services (accounts, transactions, ledger, and a payment agent) over a single MongoDB Atlas database (`leafy_bank_bian`), fronted by a Next.js + LeafyGreen UI. Every backend endpoint follows BIAN Service Domain naming and a verb-in-URL contract; the UI talks to them through a path-segment-routed proxy.

The defining story is the **two-phase money flow**: a payment executes synchronously as one MongoDB ACID transaction, then an asynchronous, change-stream-driven pipeline posts it through the general ledger — no polling, debits always equal credits.

On top of that sits the **Payments Lifecycle**: a nine-stage, ISO 20022-aligned workflow for outgoing and incoming payments, from initiation to reconciliation and exceptions, with two LangGraph agents (cut-off risk and reconciliation investigation). See [Payments Lifecycle](#payments-lifecycle).

Where MongoDB shines: **flexible schema** for evolving account/customer data (accounts), **multi-document ACID transactions** for consistent execution (transactions), and **change streams + aggregation pipelines** for real-time, event-driven double-entry accounting (ledger) — all in one database, no separate message bus.

## Why MongoDB

Legacy cores lock accounting behind COBOL batch cycles: the general ledger reconciles overnight, so balances and books drift apart intraday. The scale of the debt is well documented — 90% of US banking core software is considered legacy, and 43% of US core systems still run on COBOL. A legacy core also typically splits the problem across three systems: a relational database for balances, a CDC connector (Debezium, Kafka Connect) to move executed transactions downstream, and a separate ledger or event-store product for the accounting entries. MongoDB collapses all three into one platform:

- **Multi-document ACID transactions replace two-phase commit orchestration.** Debit, credit, transaction fact, notification, and status flip are one `session.with_transaction` call — no saga, no compensating-transaction logic.
- **Change streams replace the CDC connector.** The ledger service tails `transactions` natively, with resume tokens for exactly-once-effective delivery across restarts — no Debezium, no Kafka topic to operate.
- **The document model replaces the ledger's join tables.** A `ledgerEvent` embeds both its debit and credit legs, and a `journalEntry` embeds its full array of posting lines — one balanced accounting entry is one document, one atomic read or write.
- **Validators replace application-layer invariant checks.** The Pacioli balance rule (Σdebit == Σcredit) is a `$expr` validator on `journalEntries` itself; required-field and enum rules on `ledgerEvents`/`subLedgerEntries` are JSON Schema validators. The invariant holds even for a write that bypasses the service layer.

## What This Demo Shows

### BIAN Service Domains implemented


| Service                 | BIAN Service Domain                                  | Responsibility                                               |
| ------------------------- | ------------------------------------------------------ | -------------------------------------------------------------- |
| **accounts** (8001)     | `PartyReferenceDataDirectoryEntry`, `CurrentAccount` | Customers + KYC, account lifecycle, source-of-truth balances |
| **transactions** (8002) | `PaymentOrderInitiation`                             | Payments/transfers as one multi-doc ACID execution          |
| **ledger** (8003)       | `FinancialAccounting` / `FinancialBookingLog`        | Async double-entry general-ledger pipeline                   |
| **payment_agent** (8004) | Agents operating over the domains above             | Reconciliation and Cut-off agents (LangGraph over Bedrock)   |

### The two-phase money flow

1. **Synchronous execution** — a payment is one MongoDB **ACID transaction**: `$inc` debtor down, `$inc` creditor up, insert the transaction, flip the payment to SETTLED, write the sender notification. Idempotent via an `Idempotency-Key` header.
2. **Asynchronous general ledger** — **change-stream** workers react to executed transactions with no polling: `transactions → ledgerEvents → subLedgerEntries`, then a periodic batch rolls them into balanced `journalEntries` (debits == credits enforced at the DB level). The UI traces a payment through all stages live via `/pipeline/trace/{paymentId}`.

## Payments Lifecycle

The payments workflow extends the money flow into a full payment lifecycle: domestic and cross-border, outgoing and incoming, aligned to ISO 20022 messaging and organized by BIAN v14 service domains. **It covers wires and internal transfers.** Rail submission, correspondent responses and external settlement run against built-in simulators, so the full lifecycle runs end to end in a demo environment.

**Design in one line:** one canonical `payments` document per payment (a single `paymentId`, with a typed `wireDetails` / `internalDetails` branch, not one collection per rail) + rail adapters (the ISO 20022 message is built only at the rail boundary) + an append-only lifecycle history in `paymentHistory`.

*Internal transfers are book transfers: they settle at the money move (stage 5), post at stage 6, and reconcile with a single Payment-to-GL comparison. External wires settle at stage 7. Stage 9 is modelled as a state and action on the originating payment, with the queue in the `exceptions` collection.*

### What happens at each stage

Stage names, order and colours follow the payment lifecycle screens in the UI (green: customer-facing stages, blue: ledger and settlement, purple: reconciliation and exceptions). Stages marked *Agentic AI* have an agent attached.

**Outgoing payment** (`/payments` overview)

```mermaid
flowchart LR
  A1["1<br/><b>Payment Initiation</b><br/>Create the payment instruction"]:::green --> A2["2<br/><b>Authentication &<br/>Authorization</b><br/>Verify customer and permissions"]:::green --> A3["3<br/><b>Validate & Enrich</b><br/>Validate the instruction and enrich data"]:::green --> A4["4<br/><b>Orchestration &<br/>Approval</b><br/>Score fraud, pick the rail,<br/>prepare ISO 20022<br/><i>Agentic AI</i>"]:::green --> A5["5<br/><b>Payment Execution</b><br/>Send and receive confirmations"]:::green --> A6["6<br/><b>Accounting & Posting</b><br/>Ledger events, sub-ledger, GL"]:::blue --> A7["7<br/><b>Clearing &<br/>Settlement</b><br/>Settlement and position updates"]:::blue --> A8["8<br/><b>Reconciliation</b><br/>Match across systems and<br/>find discrepancies<br/><i>Agentic AI</i>"]:::purple --> A9["9<br/><b>Exceptions / Returns</b><br/>Investigate, resolve and complete"]:::purple
  classDef green fill:#E3FCF7,stroke:#00A35C,color:#001E2B
  classDef blue fill:#E1F7FF,stroke:#016BF8,color:#001E2B
  classDef purple fill:#F9EBFF,stroke:#7C3AED,color:#001E2B
```

**Incoming payment** (`/payments-workflow` deep dive)

```mermaid
flowchart LR
  B1["1<br/><b>Payment Received</b>"]:::green --> B2["2<br/><b>Message Authentication &<br/>Beneficiary Resolution</b>"]:::green --> B3["3<br/><b>Validation & Enrichment</b>"]:::green --> B4["4<br/><b>Acceptance Decision &<br/>Compliance Authorization</b>"]:::green --> B5["5<br/><b>Execution<br/>(Status Response)</b>"]:::green --> B6["6<br/><b>Accounting & Posting</b>"]:::blue --> B7["7<br/><b>Clearing & Settlement</b>"]:::blue --> B8["8<br/><b>Reconciliation</b><br/><i>Agentic AI</i>"]:::purple --> B9["9<br/><b>Exceptions / Returns</b>"]:::purple
  classDef green fill:#E3FCF7,stroke:#00A35C,color:#001E2B
  classDef blue fill:#E1F7FF,stroke:#016BF8,color:#001E2B
  classDef purple fill:#F9EBFF,stroke:#7C3AED,color:#001E2B
```

**Outgoing payment**

| Stage | What happens |
| --- | --- |
| 1. Initiation | A customer, back-office user or API submits a payment instruction. The system records it as a canonical payment with a unique `paymentId`. Malformed requests are refused before a payment exists. |
| 2. Authentication & Authorization | The system confirms who is paying and that they may make this payment: account ownership and entitlement limits, with step-up authentication where the amount requires it. |
| 3. Validation & Enrichment | The instruction is checked for completeness, duplicates (idempotency) and sufficient funds. The system adds supporting data such as FX and payment type, and reserves the funds so they cannot be spent elsewhere. |
| 4. Orchestration & Approval | The system chooses the route (rail and cut-off window) and records it as an immutable routing snapshot. Fraud and sanctions checks run, and the payment is authorised and approved. Suspicious payments pause for manual review. |
| 5. Execution | The money moves. The customer is debited and the payment is sent on its rail as an ISO 20022 `pacs.008` message. Internal transfers complete at this point. |
| 6. Accounting / Posting | The ledger records the payment as balanced double-entry postings: ledger event, sub-ledger entries, then journal entries. |
| 7. Clearing & Settlement | For wires, the correspondent or network confirms settlement and the payment moves to settled. A settlement position records expected against actual, including delays and returns. |
| 8. Reconciliation | The payment, the rail record and the general ledger are matched three ways. A clean match marks the payment reconciled. A mismatch goes to Exceptions. |
| 9. Exceptions & Returns | Any problem found along the way becomes an exception on the payment (settlement problems, reconciliation differences, duplicates). Operators, or the Reconciliation agent with human approval, resolve it or return the funds. |

**Incoming payment**

| Stage | What happens |
| --- | --- |
| 1. Receipt | A `pacs.008` arrives from another bank. The raw message is stored first, then a canonical payment is created. Duplicates are caught before a payment exists. |
| 2. Authentication & Beneficiary match | The message is checked for legitimacy, and the beneficiary is matched to a Leafy Bank account as matched, partial or no match. A no match goes to the Unable-to-Apply queue. |
| 3. Validation | Originator sanctions and AML screening run, FX is applied where currencies differ, and the payment is classified as domestic or cross-border. |
| 4. Acceptance decision | The match and screening results roll up into a single decision: accept or reject. |
| 5. Status response | A `pacs.002` status goes back to the sending bank, accepted or rejected with a reason code. |
| 6. Posting | The customer is credited. The ledger debits Wire Clearing and credits Customer Deposit Liability. |
| 7. Settlement | Settlement is confirmed. The ledger debits Nostro and credits Wire Clearing. |
| 8. Reconciliation | The same three-way match as outgoing, with the debit and credit legs mirrored. |
| 9. Unable to Apply | Payments that cannot be applied wait for an operator. **Repair** corrects the beneficiary and resumes at stage 3. **Return** sends the funds back with a `pacs.004` and closes the payment as returned. |

### Lifecycle states

- **`lifecycle.state`**: `DRAFT` → `INITIATED` → `VALIDATED` → `ENRICHED` → `FINAL_VALIDATED` → (`MANUAL_FRAUD_REVIEW` on a fraud REVIEW hold) → `ROUTED` → `AUTHORISED` → `APPROVED` → `SUBMITTED` → `IN_PROGRESS` → `SETTLED` → `RECONCILED`.
- **Independent axes**: `postingStatus` (PENDING / POSTED / FAILED), `settlementStatus` (PENDING / SETTLED / FAILED / RETURNED), `reconciliationStatus` (RECONCILED / DISCREPANT / PENDING).
- **Terminal outcomes**: `REJECTED`, `FAILED`, `RETURNED`, `CANCELLED`, `REVERSED`, `REFUNDED`.
- **Inbound adds to the same enum** (no parallel one): `RECEIVED` (parsed into a canonical payment), `ACCEPTED` (positive pacs.002 sent), and `RETURNED` meaning an accepted payment later returned via pacs.004. `REJECTED` means never accepted.
- Every transition records the new state, reason, actor and timestamp.

### BIAN v14 mapping

BIAN supplies business-capability and service-domain semantics; ISO 20022 supplies message semantics (`pain.001` for the wire initiation envelope, `pacs.008` interbank instruction, `pacs.002` status, `pacs.004` return). BIAN does not dictate the collection names below.

| Stage | BIAN service domain(s) | Collections written |
| --- | --- | --- |
| 1 Initiation | `PaymentOrderInitiation` (42933) | `payments`, `paymentHistory` |
| 2 Authentication & Authorization | `PartyAuthentication` (38917), `CustomerAccessEntitlement` (43057) | check results on `payments` |
| 3 Validation & Enrichment | `PaymentOrderInitiation` | `payments` (validation, `idempotency`, `fx`, enrichment) |
| 4 Orchestration & Approval | `PaymentOrchestration` (48782), `FraudEvaluation` (44625), `TransactionAuthorization` (43343) | `routingSnapshots` (immutable), `payments.fraud` |
| 5 Execution | `PaymentRail` (47741), `FinancialGateway` (30542) | `paymentExecutions`, `canonicalJsonStorage` (pacs.008), `transactions` |
| 6 Accounting / Posting | `FinancialAccounting` (39161), `PositionKeeping`, `CurrentAccount` | `ledgerEvents`, `subLedgerEntries`, `journalEntries` |
| 7 Clearing & Settlement | `PaymentSettlement` (40033), `InternalBankAccount` (29306), `CorrespondentBankDirectory` | `settlementPositions`, second ledger event |
| 8 Reconciliation | `AccountReconciliation` (35449), `PaymentRail` | `reconciliationItems` |
| 9 Exceptions & Returns | State and action on the originating payment | `exceptions`, `canonicalJsonStorage` (pacs.004) |

Stage 4 authorization uses the BIAN v14 `TransactionAuthorization` domain (43343).

**Incoming differs at stages 1 to 5** (BIAN domains and stored fields); stages 6 to 8 reuse the outgoing machinery with the debit and credit legs mirrored:

| Stage | Incoming (wire) |
| --- | --- |
| 1 | `FinancialGateway` + `PaymentOrderInitiation`: the raw pacs.008 is stored in `canonicalJsonStorage` first, then the `payments` doc is created (`direction: INBOUND`, `RECEIVED`). Duplicates are caught before a payment exists |
| 2 | Message authentication and beneficiary resolution (`beneficiaryResolution`: MATCHED / PARTIAL / NO_MATCH). NO_MATCH goes to the Unable-to-Apply queue |
| 3 | Originator sanctions/AML screening, simulated FX, domestic vs cross-border classification |
| 4 | `PaymentConfirmation`: roll up match and sanctions into ACCEPT / REJECT (`acceptanceDecision`) |
| 5 | Simulated pacs.002 sent to the sending bank (ACCP, or RJCT with reason code) |

### Agentic AI

Two agents run in the `payment_agent` service (LangGraph over Amazon Bedrock, graph state persisted with `MongoDBSaver`). They do not replace the deterministic workflow: validation, posting and settlement stay with the payment services, and each agent has a fixed, code-enforced action list.

| Agent | Where it acts | What it does |
| --- | --- | --- |
| **Cutoff Risk & Exception Management** | Held wires before and after the internal cut-off (stage 4) | Compares the payment to the internal and external cut-offs (ET: domestic 17:30 / 18:45, international USD 18:15 / 18:45), investigates the blocker, and acts on its own only for reminders, backup-approver escalation, screening priority and a customer funds notice. Hold to next value date, EXPEDITE and defer to next business day wait for human approval |
| **Reconciliation Mismatch Investigation** | Stage 8 exceptions (`RECONCILIATION_DISCREPANCY`, `RECONCILIATION_MISSING`, `ORPHANED_SETTLEMENT`) | The reconciliation engine decides whether records match; the agent classifies the cause (timing lag, fee, reference or amount mismatch, orphan), rechecks on its own, and proposes an action that a Payments Operations user approves. It writes only `exceptions.agent{}` and never edits ledger or journal records; corrections go through the owning service |

Walk both in the browser at `/showcase`: six reconciliation scenarios and two cut-off scenarios.

### Payment collections

`payments` is the authoritative business payment; `canonicalJsonStorage` holds the rail message and conversion audit. Accounting begins at `ledgerEvents`; debit and credit legs are created by the ledger service.

```
outbound:  payments -> routingSnapshots -> paymentExecutions (+ canonicalJsonStorage) -> transactions -> ledgerEvents -> ...
inbound:   canonicalJsonStorage -> payments -> ... -> transactions -> ledgerEvents -> ...
```

### Scope

In scope: wire and internal-transfer payments, outbound and inbound, with simulated rails and screening. Rail access goes through an adapter (`simulated_wire_rail`), and ISO 20022 messages are built only at that boundary.

## Where Does MongoDB Shine?

Leafy Bank demonstrates the power and flexibility of MongoDB, making it an ideal choice for financial services applications. By leveraging MongoDB's advanced features, the backend microservices are designed to handle complex banking operations efficiently and securely.

This modern **microservices architecture** splits functionalities across different repositories, showcasing a real-world approach to scalable and maintainable software development. Here's how MongoDB shines in the backend services powering Leafy Bank:

### 1. **Accounts Service**

**[Accounts Service](backend/accounts/)**
This service handles account operations. MongoDB excels here by offering a **flexible schema**, allowing the system to adapt to evolving account data structures without requiring disruptive migrations. Its scalability and adaptability ensure real-time updates and a seamless account management experience.

![accounts diagram](diagrams/accounts_diagram.png)

---

### 2. **Transactions Service**

**[Transactions Service](backend/transactions/)**
Responsible for handling digital payments and account-to-account transfers, this service uses MongoDB's **multi-document ACID transactions** to ensure reliable and consistent financial operations. This guarantees the integrity and correctness of data across multiple collections, making MongoDB a trusted choice for critical workflows within banking systems.

![transactions diagram](diagrams/transactions_diagram.png)

---

### 3. **Ledger Service**

**[Ledger Service](backend/ledger/)**
Powers the asynchronous general-ledger pipeline behind every payment — this is the demo's centerpiece. MongoDB **change streams** let this service react to executed transactions in real time — without polling — feeding a three-stage, event-driven flow that derives ledger events, projects double-entry sub-ledger entries, and rolls them up into journal entries. Combined with **multi-document ACID transactions** to keep debits and credits balanced and **aggregation pipelines** for periodic batch posting, MongoDB delivers the consistency and event-driven processing that core accounting systems demand.

![ledger service diagram](diagrams/ledger_service.png)

*Per executed payment: 1 `ledgerEvent`, 2 `subLedgerEntries` (debit + credit). Per batch cycle: 1 `journalEntry` per reconciled `(periodCode, controlAccountCode)` group, aggregating however many sub-ledger entries fell in it. `REALTIME`-mode events skip the batch and post their journal inline from `projection_worker` instead.*

**Stage 1 — Change-stream CDC ingest with resume tokens.** The ledger service watches the `transactions` collection with a change stream. Each inserted transaction fires the ingest worker, which derives the debit and credit legs from posting rules and writes one `ledgerEvent`. The worker persists the change stream's resume token after every processed event, so a restart replays nothing and misses nothing. The insert is idempotent on `paymentId` — a duplicate event hits the unique index and is skipped, never double-posted.

```python
while True:
    resume_token = load_resume_token(connection, db_name)
    kwargs = {"resume_after": resume_token} if resume_token else {}
    try:
        with transactions.watch([{"$match": {"operationType": "insert"}}], **kwargs) as stream:
            for change in stream:
                process_transaction(change["fullDocument"], connection, db_name, coa)
                save_resume_token(connection, db_name, change["_id"])
    except OperationFailure as exc:
        if exc.has_error_label("NonResumableChangeStreamError") and resume_token:
            clear_resume_token(connection, db_name)  # token aged out of oplog
            continue
        raise
```

Wrapping the open-and-iterate cycle in a loop matters: a stream that falls behind the oplog window raises `NonResumableChangeStreamError` from a `getMore` on an already-open cursor, not only at open time. The loop gives both cases the same clear-token-and-reopen recovery.

**Stage 2 — Projection to balanced sub-ledger entries.** The projection worker watches `ledgerEvents` and fans each event into two `subLedgerEntries` — one debit, one credit — written together in an ACID transaction. Before writing, it re-verifies that the debit and credit amounts match and that both GL accounts are active posting leaves in the chart of accounts. A JSON Schema validator on the collection rejects any entry that violates the stored shape. Events with a `REALTIME` posting mode post their journal inline here; `BATCH` events wait for the scheduled batch.

**Stage 3 — Batched journal posting with a reconciliation gate.** The GL batch worker runs on a fixed interval. Each cycle first reconciles every account; if any account fails to balance, the worker skips the cycle rather than post a suspect journal. On success, it aggregates pending sub-ledger entries by group into balanced `journalEntries`, stamps the `journalEntryId` back onto the source events, and flips their status to `POSTED`. The reconciliation gate makes an unbalanced batch impossible to publish.

```python
def run_one_cycle(connection, db_name, coa):
    if not reconcile_ok(connection, db_name):
        return {"skipped": True, "reason": "pre-batch reconciliation break"}
    written = run_batch(connection, db_name, coa=coa)
    return {"skipped": False, "written": written}
```

See [Indexing and validators (ledger service)](#indexing-and-validators-ledger-service) below for how idempotency, the partial index, the batch-sweep index, and the three JSON Schema/`$expr` validators back every stage above.

---

Put together, the three services form one high-level flow around a single MongoDB Atlas cluster: the accounts and transactions services execute payments and expose balances synchronously, while the ledger service reacts asynchronously to a change stream on `transactions` and posts the GL collections without the other two ever writing to them directly.

![High level architecture](diagrams/core_banking_hld.png)

*Accounts and transactions execute synchronously against MongoDB Atlas; the ledger service consumes a change stream on `transactions` and posts the GL collections asynchronously.*

### MongoDB collections


| Collection         | Backs BIAN object                | Notes                                                                              |
| -------------------- | ---------------------------------- | ------------------------------------------------------------------------------------ |
| `customers`        | PartyReferenceDataDirectoryEntry | Customer master + nested KYC                                                       |
| `accounts`         | CurrentAccountFacility           | Source-of-truth balances                                                           |
| `payments`         | PaymentOrderInitiation           | Canonical payment (outbound and inbound), lifecycle state plus posting, settlement and reconciliation axes |
| `paymentHistory`   | PaymentOrderInitiation           | Insert-only lifecycle events and checks, one doc each, written with the `payments` update |
| `routingSnapshots` | PaymentOrchestration             | Immutable route chosen at stage 4                                                  |
| `paymentExecutions`| PaymentRail                      | One doc per outbound execution attempt                                             |
| `canonicalJsonStorage` | FinancialGateway / PaymentRail   | pacs.008 / pacs.002 / pacs.004 and simulated settlement or statement messages     |
| `settlementPositions` | PaymentSettlement             | Expected vs actual settlement, outcome matched / unmatched / delayed / exception   |
| `reconciliationItems` | AccountReconciliation         | One doc per comparison leg, written by the ledger service                          |
| `exceptions`       | Originating payment's domain     | Exception queue (settlement, reconciliation, duplicate, UTA); agents annotate `agent{}` |
| `cutoffCases`      | Cut-off agent                    | Cutoff agent cases (owned by `payment_agent`)                                      |
| `transactions`     | CurrentAccountFacility           | Executed payment legs; listed via`CurrentAccountTransaction/Request` (accounts svc) |
| `glAccounts`       | FinancialAccounting              | Chart of accounts, consulted by every GL stage below                               |
| `ledgerEvents`     | FinancialBookingLog              | Stage ①: debit + credit legs, postingStatus PENDING → POSTED                     |
| `subLedgerEntries` | FinancialAccounting              | Stage ②: two entries per event (DR + CR)                                          |
| `journalEntries`   | FinancialAccounting              | Stage ③: batched, balanced (Pacioli enforced)                                     |

**How the collections chain together** — no `$lookup`-style foreign keys, just a carried-forward identifier at each stage: `payments.paymentId` → (`routingSnapshots`, `paymentExecutions`, `paymentHistory`, `settlementPositions`, `reconciliationItems`, all keyed by `paymentId`) → `transactions.paymentId` → `ledgerEvents.sourceReference.sourceId` → `subLedgerEntries.sourceReference.sourceId` → `journalEntries.journalId`, which then gets stamped back onto `subLedgerEntries.journalEntryId` and `ledgerEvents.postingResult.journalEntryId`. Every hop is a single indexed lookup — that's what lets `/pipeline/trace/{paymentId}` assemble the full trace without an aggregation-pipeline join.

### Indexing and validators (ledger service)

`backend/ledger/data/ensure_indexes.py` is the source of truth for the GL collections' indexes and validators:

- **Idempotency** — a unique index on `idempotencyKey` on `ledgerEvents`, `subLedgerEntries`, and `journalEntries`. A duplicate insert raises `DuplicateKeyError` and the worker skips it, instead of racing on check-then-insert.
- **Partial index** — on `subLedgerEntries.journalEntryId` with `{"$gt": ""}`, so the `""` sentinel written before the batch stamps the real ID stays out of the index.
- **Compound batch sweep** — `idx_batch_sweep` covers the batch query's `{status, journalEntryId}` match and its `{periodCode, controlAccountCode}` grouping in one index.
- **Multikey reconciliation** — an index on `journalEntries.entries.accountCode` bounds the reconciliation aggregation to monthly volume.
- **Three JSON Schema / `$expr` validators** — `ledgerEvents` and `subLedgerEntries` require their business fields and lock `postingStatus`/`side`/`status` to known enums; `journalEntries` enforces the Pacioli invariant (Σdebit == Σcredit) directly with `$expr`, so a write that bypasses the service layer still cannot corrupt the books.

---

By adopting a **microservices architecture**, Leafy Bank splits features across multiple repositories. This design not only supports **scalability**, **modular development**, and **independent deployments** but also underscores MongoDB's versatility in driving dynamic and robust systems.

This approach reflects a **modern and practical way to develop software**, supporting the scalability, modularity, and maintainability required for financial services in today's fast-evolving world.

## Tech Stack

- **[MongoDB Atlas](https://www.mongodb.com/atlas)** — operational data layer. Uses ACID multi-document transactions, change streams, JSON Schema validators, and aggregation pipelines. **A replica set is required** (change streams and multi-doc transactions do not work on a standalone server).
- **[FastAPI](https://fastapi.tiangolo.com/)** (Python `>=3.10,<3.11`, [Poetry](https://python-poetry.org/)-managed) — the four backend services.
- **[LangGraph](https://langchain-ai.github.io/langgraph/)** over **Amazon Bedrock** (default model `us.anthropic.claude-haiku-4-5-20251001-v1:0`, override with `BEDROCK_MODEL_ID`) — the payment agents, with `MongoDBSaver` checkpointing in Atlas.
- **[PyMongo](https://pymongo.readthedocs.io/)** — synchronous MongoDB driver.
- **[Next.js 15](https://nextjs.org/)** App Router + **[React 19](https://react.dev/)** — the frontend (JavaScript, not TypeScript).
- **[LeafyGreen UI](https://github.com/mongodb/leafygreen-ui)** (MongoDB's design system) + **[CSS Modules](https://github.com/css-modules/css-modules)** — component-scoped styling.
- **[Docker](https://www.docker.com/)** + **[Drone CI](https://www.drone.io/)** → **Kanopy** (MongoDB internal Kubernetes) — build and deploy.

## Prerequisites

Before you begin, ensure you have the following:

- **Python** `>=3.10,<3.11` and **[Poetry](https://python-poetry.org/docs/#installation)** — for the backend services.
- **Node.js** (LTS) and **npm** — for the frontend and the BIAN data-model explorer.
- **[MongoDB Atlas](https://www.mongodb.com/atlas)** cluster, or any **replica-set** deployment. A standalone `mongod` will not work — change streams and multi-document transactions both require a replica set.
- **Docker & Docker Compose** (optional, for containerized runs).

## Quick Start

```bash
# 1. Install dependencies for every service (backends via Poetry, frontends via npm)
make setup

# 2. Create a .env per backend service and frontend/.env.local — see Environment variables below

# 3. Create indexes and validators: the GL collections (ledger) and the payment
#    collections (transactions, e.g. paymentHistory) — see "Indexing and validators"
cd backend/ledger && poetry run python -m data.ensure_indexes && cd ../..
cd backend/transactions && poetry run python -m data.ensure_indexes && cd ../..

# 4. Start everything
make dev
```

`make dev` frees the dev ports, then starts the accounts, transactions, ledger and payment_agent backends plus the frontend together. Open [http://localhost:3000](http://localhost:3000).

You can also run pieces individually:

```bash
make dev-accounts      # accounts backend (8001)
make dev-transactions  # transactions backend (8002)
make dev-ledger        # ledger backend (8003)
make dev-agent         # payment agent (8004)
make dev-frontend      # frontend (3000)
make kill-ports        # free the dev ports if they are stuck
```

### Seed Data

Sample data ships in `backend/data/sample/`. The one-command loader is
[`backend/data/load_sample_seed.py`](backend/data/load_sample_seed.py) — it upserts all
four collections by business key (inserts new rows, applies new columns only to the
demo's own rows, never overwrites runtime balances; dry-run by default):

```bash
export MONGODB_URI="mongodb+srv://..."          # the service's connection string
python backend/data/load_sample_seed.py --db fsi-bian-test-db --apply
```

`--db` is your `LEAFYBANK_DB_NAME`: `fsi-bian-test-db` for local dev, `leafy-bank-bian`
in the Kanopy `environment/*.yaml`.

(The same files can be imported individually with [MongoDB Compass](https://www.mongodb.com/products/tools/compass) or `mongoimport`, but the script is idempotent and safe to re-run after a stage adds rows.)


| File                                | Collection     |
| ------------------------------------- | ---------------- |
| `leafy_bank_bian.customers.json`    | `customers`    |
| `leafy_bank_bian.accounts.json`     | `accounts`     |
| `leafy_bank_bian.transactions.json` | `transactions` |
| `leafy_bank_bian.glAccounts.json`   | `glAccounts`   |

> **_Note:_** The pipeline collections — `payments`, `ledgerEvents`, `subLedgerEntries`, `journalEntries`, `notifications` — are created at runtime as payments flow through. No seed needed.

### Environment variables

Each backend service reads its config from a `.env` in its own directory. Create one per service using the template below.

**`backend/accounts/.env`**, **`backend/transactions/.env`**, **`backend/ledger/.env`:**

```bash
MONGODB_URI=                       # required — your replica-set / Atlas connection string
LEAFYBANK_DB_NAME=leafy_bank_bian  # optional, this is the default

# transactions only:
PAYMENT_LIMIT_USD=1000000          # optional, malformed-input bound (default 1000000).
                                   # NOT the entitlement limit — stage 2 decides that per
                                   # customer segment (entitlement_policy.py). Overridable
                                   # per segment via ENTITLEMENT_PER_PAYMENT_LIMIT_<SEGMENT>,
                                   # ENTITLEMENT_DUAL_APPROVAL_THRESHOLD_<SEGMENT>,
                                   # ENTITLEMENT_STEP_UP_THRESHOLD_<SEGMENT>.

# accounts AND transactions — BIAN PartyAuthentication (SD 38917).
# accounts SIGNS the assessment token, transactions VERIFIES it, so the value must be
# IDENTICAL in both .env files or every payment is 401.
PARTY_AUTH_SECRET=                 # optional; both services fall back to a built-in demo
                                   # secret and log a warning. HS256 is symmetric: anyone
                                   # holding this can mint a token for any customer.
PARTY_AUTH_TOKEN_TTL_SECONDS=3600  # optional, accounts only (default 3600)

# transactions only:
REQUIRE_AUTHENTICATION=false       # optional (default false). false = a request with no
                                   # token still works off the body's customerId, and
                                   # stage 2 records the authentication check as SKIP.
                                   # true = an initiate without a valid token is 401.

# ledger only:
GL_BATCH_INTERVAL_SECONDS=600      # optional, GL batch cadence (default 600)
ENABLE_CHANGE_STREAMS=true         # optional (default true)
```

**`backend/payment_agent/.env`** (see `.env.example`):

```bash
MONGODB_URI=                       # required
LEAFYBANK_DB_NAME=leafy_bank_bian  # optional, this is the default
TRANSACTIONS_BASE_URL=http://localhost:8002   # agents act through the owning services
LEDGER_BASE_URL=http://localhost:8003
ENABLE_RECONCILIATION_AGENT=false  # optional (default false); true starts the reconciliation worker
ENABLE_CUTOFF_AGENT=false          # optional (default false); acts only on showcase payments tagged
                                   # demo.clockRunId. Keep it on in ONE instance per database.
AWS_REGION=us-east-1               # Bedrock region; credentials come from the standard AWS chain
BEDROCK_MODEL_ID=                  # optional override of the default Haiku 4.5 profile
```

Optional simulation workers (all default as shown): `ENABLE_SETTLEMENT_COMPLETION=true`, `ENABLE_STATEMENT_SIM=true`, `ENABLE_INBOUND_SIM=false`, `ENABLE_CUTOFF_RELEASE_JANITOR=false` (transactions), and `ENABLE_CORRESPONDENT_REPLY_SIM=true` (ledger).

**Frontend** — create `frontend/.env.local`. The UI proxies BIAN requests to each service by path segment, so it needs the backend URLs:

```bash
ACCOUNTS_BACKEND_URL="http://localhost:8001"
TRANSACTIONS_BACKEND_URL="http://localhost:8002"
LEDGER_BACKEND_URL="http://localhost:8003"
AGENTS_BACKEND_URL="http://localhost:8004"   # payment_agent (this is also the default)
# Optional: external open-finance aggregation (out of scope for the BIAN flow)
CORE_BACKEND_URL="http://localhost:8000"
```

### Services and ports


| Service      | Port | Notes                                                      |
| -------------- | ------ | ------------------------------------------------------------ |
| accounts     | 8001 | BIAN`PartyReferenceDataDirectoryEntry` + `CurrentAccount`  |
| transactions | 8002 | BIAN`PaymentOrderInitiation` (synchronous ACID execution) |
| ledger       | 8003 | BIAN`FinancialAccounting` + `/pipeline` monitor routes     |
| payment_agent | 8004 | Reconciliation and Cut-off agents (`/reconciliation/*`, `/cutoff/*`) |
| frontend     | 3000 | Next.js UI                                                 |

> The optional [Open Finance Service](https://github.com/mongodb-industry-solutions/leafy-bank-backend-openfinance) (external-bank aggregation) is out of scope for the BIAN flow; the proxy falls back to `CORE_BACKEND_URL` for its routes.

## Using the Application

Once `make dev` is running and you've seeded data, open [http://localhost:3000](http://localhost:3000):

- **Home / Accounts** (`/`, `/accounts`) — balances, recent transactions, send-money flow.
- **Credit Cards / Loans / Portfolio** (`/credit-cards`, `/loans`, `/portfolio`) — other product surfaces backed by the same accounts service.
- **Send a payment** — from the home or accounts view, send money between two Leafy Bank accounts. Each payment click drives the two-phase flow end to end.
- **Pipeline trace** — in the transactions table, click an executed payment to watch it move live through `transaction → ledgerEvent → subLedgerEntries → journalEntry` (polled every 2s via `/pipeline/trace/{paymentId}`).
- **Payments** (`/payments`) — the lifecycle overview diagram for the payments workflow.
- **Payments Workflow** (`/payments-workflow`) — payment history list (filter by Domestic / International), a New payment wizard, a Generate incoming wire action, and a per-payment lifecycle deep dive across the nine stages with the MongoDB documents each stage writes. Exceptions, including the Unable-to-Apply queue, are handled here.
- **Operations dashboard** (`/payments-dashboard`) — throughput, lifecycle-stage counts, exceptions, settlement and reconciliation status.
- **Agent scenarios** (`/showcase`) — guided walkthroughs of the Reconciliation agent (six scenarios) and the Cut-off agent (two scenarios), including the human approval step.
- **GL Pipeline Monitor** (`/gl-pipeline-monitor`) — batch cadence, last-run time, and posting-status counts across the ledger pipeline.
- **Leafy Bank Assistant** — the floating chat bubble (bottom-right) answers questions about your accounts and transactions via the chatbot backend.

## Run with Docker

From the repository root:

```bash
make build   # build and start all containers
make logs     # tail container logs
make clean   # stop and remove containers and images
```

Other lifecycle targets: `make up`, `make start`, `make stop`, `make down`.

## Additional Resources

- [MongoDB for Financial Services](https://www.mongodb.com/solutions/industries/financial-services)
- [Change Streams](https://www.mongodb.com/docs/manual/changeStreams/) — the engine behind the async GL pipeline
- [Transactions](https://www.mongodb.com/docs/manual/core/transactions/) — multi-document ACID execution
- [Aggregation Pipelines](https://www.mongodb.com/docs/manual/aggregation/) — GL batch posting
- [BIAN](https://bian.org/) — the banking service-domain reference model this demo aligns to
- [FastAPI](https://fastapi.tiangolo.com/) · [Next.js 15](https://nextjs.org/) · [LeafyGreen UI](https://github.com/mongodb/leafygreen-ui)

## 📄 License

See [LICENSE](LICENSE) file for details.
