# Leaf Bank BIAN — Design Flow & UI Brainstorm

> Created: 2026-06-26  
> Purpose: Document the full system design flow (accounts → transactions → ledger) and UI directions for making the async bookkeeping pipeline visible in the demo UI.

---

## System Design Flow

### Accounts Service — "The Source of Truth"

Manages customers and their accounts. Key operations: open account, get balance, list transactions via `$or` fan-out across `payer.accountId`/`payee.accountId`. **Accounts are the anchor** — they hold the live balances that payments debit/credit in real-time.

GL account codes are assigned at account creation:
- Current accounts → GL code `2100` (Customer Deposits — Current)
- Savings accounts → GL code `2200` (Customer Deposits — Savings)

---

### Transactions Service — "The Money Move" (synchronous ACID)

When a payment is initiated, two things happen in strict order:

**Before the transaction** (outside ACID):
- Validation: account existence, OPEN status, currency match, sufficient balance, ownership
- `payments` doc inserted with `status: PENDING`

**Inside one ACID transaction** (5 writes, all-or-nothing):
1. `accounts[debtor]` — `$inc` balance down
2. `accounts[creditor]` — `$inc` balance up
3. `transactions` — insert the movement doc (no GL, no legs — just payer→payee + balanceAfter)
4. `notifications` — insert sender notification
5. `payments` — flip `status: PENDING → SETTLED`

The `transactions` doc is intentionally **not an accounting record** — it's a business fact. From the code comment: *"NOT an accounting record (no legs, no gl) — the ledger service derives DR/CR ledgerEvents from this via CDC."* The ledger derives everything downstream via change streams.

---

### Ledger Service — "The Accounting Machine" (3-stage async pipeline)

The ledger never touches accounts or payments directly. It watches MongoDB change streams and reacts.

```
transactions (insert)
    │  Change Stream — ingest_worker
    ▼
ledgerEvents  [postingStatus: PENDING]
    │  Change Stream — projection_worker
    ▼
subLedgerEntries  [status: POSTED, journalEntryId: ""]
    │  Scheduled batch every 10 min — gl_batch
    ▼
journalEntries  [status: POSTED]
    ↑
    └── subLedgerEntries  ← journalEntryId stamped
    └── ledgerEvents      ← postingStatus flipped to POSTED
```

---

#### Stage 1 — Ingest Worker (`transactions → ledgerEvents`)

- Watches `transactions` collection for inserts via change stream
- Looks up both payer and payee accounts from `accounts`
- Calls `decompose_principal_payment()` (posting rules + chart of accounts) to derive GL account codes
- Builds one `ledgerEvent` with a `debitLeg` + `creditLeg`
- Idempotent on `paymentId` (unique index catches replays)
- Resume token persisted in `ledgerStreamTokens` — survives restarts without replaying or missing events

---

#### Stage 2 — Projection Worker (`ledgerEvents → subLedgerEntries`)

- Watches `ledgerEvents` collection for inserts via change stream
- Fans out the single event into exactly 2 `subLedgerEntries` (DEBIT + CREDIT)
- Validates before writing:
  - Debit amount == credit amount
  - GL accounts are active posting leaves (not control accounts)
  - Entity references (accounts) exist and are ACTIVE
- Computes running balance per control account (chained, not double-read)
- Single ACID transaction: inserts both entries + stamps `postingResult` back on the `ledgerEvent`
- Idempotent via `idempotencyKey = eventId-SIDE`

---

#### Stage 3 — GL Batch (`subLedgerEntries → journalEntries`, every 10 min)

Scheduled loop. Each cycle:

1. **Pre-batch reconciliation** — if any control account breaks for the current period, **skips the entire cycle** (fail-safe)
2. **Completeness check** — only events with exactly one DEBIT + one CREDIT proceed; partial pairs are excluded with a warning
3. **Aggregation pipeline** — groups by `(periodCode, controlAccountCode, side)` → summed amounts per line
4. **Balance assertion** — Σ(DEBIT) == Σ(CREDIT) enforced in code before write (also enforced by DB-level JSON schema validator)
5. **One ACID transaction per period**: insert `journalEntry` + stamp `journalEntryId` on all subledger rows + flip `ledgerEvents.postingStatus → POSTED`

Idempotent via `idempotencyKey = JOURNAL-{batchId}-{periodCode}`.

---

## MongoDB Features Used (for reference)

| Feature | Where |
|---------|-------|
| ACID multi-doc transactions | payments settlement, subledger write, journal write |
| Change streams with resume tokens | ingest_worker, projection_worker |
| JSON Schema validators (`collMod`) | ledgerEvents, subLedgerEntries, journalEntries (Pacioli balance enforced at DB) |
| Compound + partial + sparse indexes | subLedgerEntries sweep/reconciliation queries |
| `$inc` atomic balance updates | payments_service debtor/creditor balance move |
| Aggregation pipelines | journal batch grouping, reconciliation signed sums, completeness checks |
| `DuplicateKeyError` idempotency | all three write boundaries |
| `Int64` / `Decimal128` BSON types | all money fields |
| `distinct()` | reconciliation account scan |
| `find_one_and_update` with `return_document=True` | atomic read-modify for balanceAfter |

---

## UI Brainstorm — Showing the Async Pipeline

Goal: make the invisible async bookkeeping pipeline *visible* and educational in real-time.

---

### Option 1 — Live Pipeline Ticker ⭐ (Recommended lead)

A horizontal pipeline diagram with 4 labeled stage nodes:

```
[ Payment ] ──► [ Ledger Event ] ──► [ Sub-Ledger ] ──► [ Journal Entry ]
  SETTLED          PENDING→POSTED       DR + CR           Batch pending...
```

When a payment is initiated, an animated dot travels along the pipeline. Each stage node lights up with a status badge as the underlying collection document is created/updated. Key UX details:
- Payment → SETTLED: immediate (synchronous)
- Ledger Event: appears within ~1-2 seconds (CDC latency)
- Sub-Ledger: DEBIT + CREDIT entries appear shortly after
- Journal: shows a countdown timer ("next batch in 6:42") until the batch fires

**Why compelling:** makes MongoDB change streams and the batch scheduler tangible — you can literally watch the system work in real-time.

---

### Option 2 — Double-Entry T-Account Visualizer

When clicking into any payment detail, show the accounting decomposition as a T-account:

```
        DEBIT                    CREDIT
┌──────────────────┐    ┌──────────────────┐
│ Customer Deposits│    │ Customer Deposits │
│   (Acct 2100)    │    │   (Acct 2200)    │
│                  │    │                  │
│    $500.00       │    │    $500.00       │
└──────────────────┘    └──────────────────┘
        ↑ must balance ↑
```

Tooltip on hover: "This means the bank's liability to Alice decreased by $500, and its liability to Bob increased by $500 — the bank's total obligations didn't change." Makes double-entry bookkeeping intuitive without jargon.

---

### Option 3 — Ledger Pipeline Health Panel

A sidebar or dedicated "Ledger" tab showing live counts:

| Metric | Value |
|--------|-------|
| Transactions awaiting ledger event | 0 |
| Ledger events PENDING projection | 2 |
| Sub-ledger entries awaiting journal | 14 |
| Reconciliation status (2026-06) | ✅ OK |
| Next GL batch | 6:42 |
| Last journal posted | 3 min ago |

Works well as a "bank operations console" feel for technical audiences.

---

### Option 4 — Per-Payment Event Timeline ⭐ (Recommended depth layer)

On the payment detail screen, a vertical timeline showing the exact event sequence with real timestamps:

```
✅  14:23:01.042   Payment SETTLED          (ACID — balance moved atomically)
⏳  14:23:01.891   Ledger Event created     (CDC picked up insert, 849ms lag)
✅  14:23:01.934   Sub-Ledger projected     (DR $50000 / CR $50000, minor units)
⏸  14:23:01.934   Awaiting GL batch...     (next run in 6:42)
✅  14:30:00.118   Journal Entry posted     (BATCH-20260626T1430, period 2026-06)
```

Shows the async nature explicitly — the gap between sub-ledger and journal is the 10-min batch window, visible as a real wait time.

---

### Option 5 — "What Just Happened" Explainer Drawer

After any payment, a slide-in drawer with plain-English explanation targeted at non-banking audiences:

> **Your $500 transfer is complete.**
>
> The money moved instantly — Alice's balance decreased and Bob's balance increased in a single atomic operation.
>
> In the background, Leafy Bank's accounting engine is recording this as a formal bookkeeping entry. The ledger picked up the transaction, classified it against the chart of accounts, and will include it in the next general ledger summary batch (runs every 10 minutes).
>
> This two-phase design is how real banks separate the "money moved" fact from the "accounting recorded" fact.

---

## Recommended UI Direction

**Lead with Option 1 (Pipeline Ticker) + Option 4 (Event Timeline).**

- The ticker makes the system architecture the visual hero of the demo — it shows MongoDB change streams doing real work in real-time, which is the MongoDB story
- The timeline on the payment detail screen provides depth for technical audiences who want to drill in
- Option 3 (Health Panel) works as a bonus "ops console" tab for engineering-heavy audiences
- Option 2 (T-Account) is the right explainer for finance/accounting audiences

**What to build first:** The pipeline ticker requires polling or a websocket to the ledger pipeline status endpoint (`/pipeline/status` aggregates `postingStatus` counts). The event timeline is simpler — it's a read from `ledgerEvents` + `subLedgerEntries` + `journalEntries` filtered by `paymentId`.

---

## Open Questions for Next Session

- [ ] Does the frontend have a WebSocket or SSE mechanism already, or does it poll?
- [ ] Which audience does this demo target primarily — technical (MongoDB features) or banking (BIAN/accounting)?
- [ ] Should the pipeline ticker be always-visible (e.g. dashboard widget) or only visible during/after a payment action?
- [ ] Is the 10-min batch interval configurable for demo purposes (e.g. set to 30s to make the journal step visually snappy)?
