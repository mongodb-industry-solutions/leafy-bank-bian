# Ledger Service — GL Pipeline

The ledger service is a pure consumer of the transactions service. It drives accounting through three sequential async stages, never touching `accounts` or `payments` directly.

## Pipeline overview

```
transactions (insert)
  → [ingest_worker]     → ledgerEvents       (postingStatus=PENDING)
  → [projection_worker] → subLedgerEntries   (status=POSTED, journalEntryId="")
  → [gl_batch]          → journalEntries     (status=POSTED)
                           ↑ flips ledgerEvents.postingStatus=POSTED
                           ↑ stamps subLedgerEntries.journalEntryId
```

---

## Startup (`main.py`)

On boot, the FastAPI `lifespan` context does three things:

1. **Load Chart of Accounts** — `ChartOfAccounts.from_db()` reads the CoA into memory once. All workers share this in-process cache; no per-event DB reads for GL account lookups.
2. **Launch workers as daemon threads** — each worker runs in a `threading.Thread(daemon=True)` wrapped by `_restart_loop`, which catches any exception and restarts after 5 seconds.
3. **Env var controls**:
   - `ENABLE_EMBEDDED_WORKERS=false` — skip all workers (Docker Compose runs them as separate containers).
   - `ENABLE_CHANGE_STREAMS=false` — skip the two CDC workers but still run the batch.
   - `GL_BATCH_INTERVAL_SECONDS` — batch cadence (default 600 s / 10 min).

---

## Stage 1 — `ingest_worker.py`

**Trigger:** MongoDB change stream on `transactions`, insert-only (`operationType: insert`).

For each new transaction:

1. Fetches the payer and payee account docs from `accounts`.
2. Calls `decompose_principal_payment()` (`posting_rules.py`) — pure, no I/O — producing two `PostingLeg` objects using the GL account code stored on each account doc (`account.gl.accountCode`).
3. Builds a `ledgerEvent` doc with both legs embedded (`debitLeg`, `creditLeg`), `postingStatus=PENDING`, and `idempotencyKey=paymentId`.
4. Inserts into `ledgerEvents`; `DuplicateKeyError` means already processed — skip.

**Accounting rule:** A customer deposit account is a bank liability (normal balance CREDIT).
- Payer (money out) → DEBIT the deposit liability (liability decreases).
- Payee (money in) → CREDIT the deposit liability (liability increases).

For an internal transfer both legs hit the same control account, netting to zero — total deposit liability unchanged.

**Resume token:** After each successful event, saves the change stream resume token to `ledgerStreamTokens` (keyed by `workerId`). On restart, picks up exactly where it left off. If the token is stale (outside the oplog window), clears it and starts a fresh stream rather than crashing.

---

## Stage 2 — `projection_worker.py`

**Trigger:** Change stream on `ledgerEvents`, insert-only.

For each new `ledgerEvent`:

1. Validates: both legs present, `debitLeg.amount == creditLeg.amount`, both GL codes are ACTIVE posting leaf accounts in the CoA.
2. Calls `build_subledger_entries()` — fans the single event into two `subLedgerEntry` docs (one DEBIT, one CREDIT), each with `idempotencyKey="{eventId}-{side}"`.
3. Computes a running balance: fetches the last `runningBalance` per control account from `subLedgerEntries`, then chains DEBIT-first so both legs on the same account (internal transfer) see each other's effect.
4. `write_subledger_entries()` runs an **ACID multi-doc transaction**:
   - `insert_many(entries)` into `subLedgerEntries`
   - `update_one` on `ledgerEvents` to stamp `postingResult` (sub-ledger IDs + timestamp)

   `postingStatus` stays `PENDING` here — `POSTED` means the journal entry is also written; the batch completes that flip.

5. On validation failure, marks `ledgerEvent.postingStatus=FAILED` with a reason and continues — does not crash the worker.

---

## Stage 3 — `gl_batch.py`

**Trigger:** Timer loop — fires every `GL_BATCH_INTERVAL_SECONDS`.

### Pre-batch reconciliation gate

Before writing anything, `reconciliation_service.reconcile_all_accounts()` checks that all accounts' sub-ledger balances reconcile for the current period. If any account breaks, the entire cycle is skipped. Fails-open on transient DB errors so a single exception does not permanently block posting.

### Batch execution (`journal_service.run_batch()`)

1. **Completeness check** — aggregates `subLedgerEntries` where `status=POSTED` and `journalEntryId=""` (the unjournaled sentinel). Groups by `sourceId` (eventId). Any event that does not have exactly 2 entries covering both sides `{DEBIT, CREDIT}` is excluded.
2. **Aggregation** — groups remaining entries by `(periodCode, controlAccountCode, side)`, summing amounts. Produces one line per distinct combination — a compact summary journal, not one line per transaction.
3. **Partitions by period** — writes one balanced `journalEntry` per calendar period if entries span multiple months.
4. **Writes each journal in an ACID transaction**:
   - `insert_one` into `journalEntries`
   - `update_many` on matching `subLedgerEntries` to stamp `journalEntryId`
   - `update_many` on matching `ledgerEvents` to flip `postingStatus=POSTED`
5. Idempotent via `idempotencyKey="JOURNAL-{batch_id}-{period_code}"`.

---

## Invariants

| Invariant | Enforced in |
|-----------|-------------|
| ΣDR == ΣCR per event | `posting_rules.assert_balanced()` at ingest |
| ΣDR == ΣCR per journal | `journal_service.assert_balanced_journal()` at batch |
| No partial projections | ACID txn wraps `subLedgerEntries` insert + `ledgerEvents` update |
| No partial journals | ACID txn wraps `journalEntries` insert + both status flips |
| No double-processing | `DuplicateKeyError` on unique indexes at every stage |
| No stale resume | Token cleared + fresh stream on `NonResumableChangeStreamError` |
| No permanently crashed workers | `_restart_loop` auto-restarts any worker after 5 s |

---

## `postingStatus` lifecycle

```
PENDING  →  (projection_worker writes subLedgerEntries)  →  PENDING (postingResult stamped)
         →  (gl_batch writes journalEntry)               →  POSTED
         →  (projection_worker validation fails)         →  FAILED
```

`POSTED` on a `ledgerEvent` means both stages completed — sub-ledger entries exist **and** a journal entry is stamped. Until the batch runs, events are `PENDING` even after projection.

---

## Key files

| File | Role |
|------|------|
| `main.py` | App bootstrap; launches workers as daemon threads |
| `workers/ingest_worker.py` | Stage 1: `transactions` CDC → `ledgerEvents` |
| `workers/projection_worker.py` | Stage 2: `ledgerEvents` CDC → `subLedgerEntries` |
| `workers/gl_batch.py` | Stage 3: timed batch → `journalEntries` |
| `shared/posting_rules.py` | Pure DR/CR decomposition; `MAPPING_VERSION` provenance stamp |
| `shared/coa_cache.py` | In-memory Chart of Accounts; shared across all workers |
| `services/subledger_service.py` | Builds + writes sub-ledger entries (ACID) |
| `services/journal_service.py` | Aggregates + writes journal entries (ACID) |
| `services/reconciliation_service.py` | Pre-batch reconciliation gate |
