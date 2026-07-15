# GL Pipeline Monitor — UI Overview (Review Meeting)

Purpose: walk the team through **what each section is, what it shows, and what each button does**. Not a pitch. Log improvements per section as you go.

Page: `/gl-pipeline-monitor` · Persona: `marcowenz / Bank Ops Admin`

---

## 1. Header
- **Logo / "View Data Model"** — link out to the BIAN data model explorer.
- **User chip (top-right)** — current persona; the refresh icon switches user.
- **"Period: May – Jul 2026"** — the reporting window everything on the page is scoped to (rolling 3-month roll-up).

> **Say:** "We're logged in as Marco, a Bank Ops Admin. Everything on this page is scoped to the period shown top-right, and 'View Data Model' jumps to the BIAN data model."

> Improvements: _______________________________________________

## 2. GL Debit / Credit / Balance by Control Account (chart, top-left)
- Bar chart, one group per control account code (2120, 2110).
- Three series: **sum(debit)**, **sum(credit)**, **sum(balance)**. Balance = debit − credit.
- Read-only; same data as the table to its right, visualized.

> **Say:** "This chart breaks down debit, credit, and net balance per control account. It's just a visual of the table next to it."

> Improvements: _______________________________________________

## 3. Top Control Accounts (table, top-center)
- Rows per control account: **Code / Name / Debit / Credit / Balance**.
- **Total row** — debit and credit totals; balance nets to **0.00** when balanced.
- Negative balance shows red (e.g. 2110 −2,448.54) — normal double-entry, not an error.
- The **"May – Jul 2026"** chip echoes the active period.

> **Say:** "Here are the top control accounts — Savings and Current — with their debits, credits, and balance. The total nets to zero, which is the ledger balancing. A red negative here is normal double-entry, not an error."

> Improvements: _______________________________________________

## 4. KPI cards (top-right, 5 tiles)
- **Total Journals** — count of posted journal entries in the period.
- **Total Debit / Total Credit** — money totals; equal when the ledger balances.
- **Out of Balance** — count of unbalanced entries (0 = healthy).
- **Reconciliation** — overall status flag (BALANCED / not).
- All read-only summary tiles.

> **Say:** "These five tiles are the at-a-glance health check: how many journals, total debit and credit — which match — anything out of balance, and the overall reconciliation status. Zero out-of-balance and BALANCED is what we want to see."

> Improvements: _______________________________________________

## 5. View toggle (two tabs)
- **Pipeline Monitor (for Multiple Transactions)** — the 4-stage feed view (default).
- **Payment Trace (for Single Transactions)** — trace one payment across all stages.
- Clicking **→ Trace transaction** on any Transactions card auto-switches to this tab for that payment.

> **Say:** "Two views: 'Pipeline Monitor' shows all transactions flowing through the four stages — that's what we're on. 'Payment Trace' lets you follow a single payment end to end."

> Improvements: _______________________________________________

## 6. Initiate Transaction panel (collapsible bar)
Header row: **"Initiate Transaction — POST to transactions service · watch the pipeline below react live."**
Controls on the header (always visible):
- **Next-batch countdown (spinner/timer)** — time until the next scheduled `gl_batch` cycle.
- **▶ Run batch** — POSTs `/pipeline/batch/trigger`; runs one GL batch cycle **on demand** instead of waiting for the timer. Journal Entries (stage 3) update after it posts.
- **↻ Refresh All** — refetches all four columns + dashboard.
- **▾ chevron** — expands/collapses the panel body.

Panel body (when expanded):
- **Real-time presets** (rail WIRE/INTERNAL) — one-click buttons that fire a preset payment; posts immediately, pipeline reacts live.
- **Batch presets** (rail ACH/VENMO/PAYPAL) — same, but batch-rail payments.
- **✏️ Custom transaction** — opens a form (Customer, Debtor, Creditor, Amount, Type, Rail, Remittance) → **Fire Transaction**.
- **⚡ Bulk transactions** — fires a randomized batch of 5 in one call.
- Result banner shows the created `paymentId` and "Waiting for pipeline… / Pipeline updated".

> **Say:** "This panel lets us push test payments in and watch the pipeline react. The countdown shows when the next GL batch runs automatically; 'Run batch' triggers one right now; 'Refresh All' reloads everything. Expand it and you get one-click presets, a custom form, and a bulk button that fires five at once."

> Improvements: _______________________________________________

## 7. Pipeline rail (numbered 0 → 3)
Visual stepper connecting the four stages. Each stage below is a live feed column.
Column header shows **count = "current / total"** — items in the last 2 batch windows / all-time. A **"current ↑ · ↓ last 2 batches"** separator divides fresh rows from prior batches.

- **0 · Transactions** — *UPSTREAM · read-only · settled payments.* The business fact. Each card has **→ Trace transaction**.
- **1 · Ledger Events** — *`ingest_worker` · change stream.* Derived debit/credit legs; each row shows **DR = CR**.
- **2 · SubLedger Entries** — *`projection_worker` · ACID txn.* Individual DR / CR entries tagged to control account (2110/2120), with running balance + journal id.
- **3 · Journal Entries** — *`gl_batch` · scheduled.* Batched, posted journals; shows batch id, period, line count, DR/CR totals.

> **Say:** "This is the core — a payment flows left to right through four stages. Stage 0 is the settled payment, the business fact. A change stream picks it up into Ledger Events with balanced debit/credit legs. The projection worker writes the actual sub-ledger entries in an ACID transaction. Finally the scheduled batch rolls them into posted journal entries. The count on each column is 'recent / all-time', and the separator line marks the last two batches."

> Improvements: _______________________________________________

## 8. Filters (Toolbar, where present)
- **Period** — text input (e.g. `2026-06`) to scope the view.
- **Status (events)** — dropdown: ALL / PENDING / POSTED / FAILED.
- **↻ Refresh All** / **Load Data** — refetch with the current filters.

> **Say:** "These filters scope what we're looking at — by period and by event status — then reload the data."

> Improvements: _______________________________________________

---

## Button quick-reference
| Button | What it does |
|---|---|
| ▶ Run batch | Runs one GL batch cycle now (`/pipeline/batch/trigger`) |
| ↻ Refresh All | Refetch all columns + dashboard |
| ▾ chevron | Expand/collapse Initiate panel |
| Real-time / Batch preset | Fire one preset payment |
| ✏️ Custom transaction | Open custom payment form |
| Fire Transaction | Submit the custom payment |
| ⚡ Bulk transactions | Fire 5 randomized payments in one call |
| → Trace transaction | Jump to Payment Trace tab for that payment |
| Period / Status filters | Scope the data shown |
