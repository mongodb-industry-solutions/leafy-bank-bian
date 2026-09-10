/**
 * The nine-stage lifecycle, as presentation stages for the horizontal rail.
 *
 * Composes the two halves of a payment's trace into ONE left-to-right saga:
 *   * stages 1-5 from /workflow/payments/{id}   (transactions service — payments doc)
 *   * stage 6 + 8 from /pipeline/trace/{id}     (ledger service)
 * Neither service reads the other's collections for accounting purposes; the join is here.
 *
 * ⚠️ **`stage:` is Doina's stage number, and stage 6 has THREE panels.** Her lifecycle is
 * 6 = Accounting/Posting, 7 = Clearing & Settlement, 8 = Reconciliation, 9 = Exceptions.
 * Until stage 6 this file numbered `ledgerEvent`/`subLedger`/`generalLedger` as 6/7/8 and
 * `reconciliation` as 9 — three presentation panels occupying three of her stage numbers,
 * which left **no slot for her stage 7 or 9** (doc 20 B4). All three accounting panels now
 * carry `stage: 6`; reconciliation is `stage: 8`. Stages 7 and 9 are deliberately empty and
 * belong to the plans that own them. Renumbering is a one-time correction — a later stage
 * still adds itself by appending, per doc 16 §5.
 *
 * Kept as a pure function (data in, plain objects out) so the rail and the detail pane both
 * read one stage model, and so a later stage adds itself by appending an entry rather than
 * editing a component. That is doc 16 §5's contract, expressed as a list.
 *
 * Deliberately NOT reusing PaymentTrace's buildStages: that one is bound to
 * GlMonitor.module.css (its `pairs` embed <Chip> elements styled by that module's CSS
 * variables), so importing it would drag GL-monitor styling into this route. The GL monitor
 * keeps PaymentTrace exactly as it is.
 */

const sum = (rows) => rows.reduce((t, r) => t + (Number(r.amount) || 0), 0);

/** Ledger amounts are minor units; the payment side stores majors. */
const minor = (v) => (v == null ? null : Number(v) / 100);

const leg = (code, name, amount) => ({ code, name, amount: minor(amount) });

/**
 * Stage 6's one-line summary. The posting axis advances independently of `currentState`
 * (spec: *"POSTED is an accounting fact, not a pipeline position"*), so this reads
 * `lifecycle.postingStatus` — written by the LEDGER service (doc 20 B1) — rather than
 * inferring posting from the pipeline state.
 */
function accountingMeta(payment, jn) {
  const posting = payment?.lifecycle?.postingStatus;
  const journalRef = payment?.refs?.journalEntryId;
  if (posting === "POSTED" && journalRef) return journalRef;
  if (posting) return posting.toLowerCase();
  if (jn) return jn.periodCode;
  // An external wire halts at IN_PROGRESS and writes no `transactions` doc, so it reaches
  // no ledgerEvent at all (execute.py:351). Say that, rather than rendering an empty panel
  // that reads like a bug.
  if (payment?.creditor?.accountId == null && payment?.rail && payment.rail !== "INTERNAL") {
    return "not yet posted — settlement pending (stage 7)";
  }
  return "awaiting the GL batch";
}

/** Stage 3's one-line summary: what enrichment actually did, or how far the stage got. */
function stageThreeMeta(payment, reached) {
  const resolved = payment?.enrichment?.resolved?.length ?? 0;
  if (resolved) return `${resolved} field${resolved === 1 ? "" : "s"} enriched`;
  if (reached("FINAL_VALIDATED")) return "validated";
  if (reached("ENRICHED")) return "nothing to enrich";
  return "stage 3";
}

function stageFourMeta(payment, reached) {
  const decision = payment?.fraud?.decision;
  const network = payment?.wireDetails?.network;

  // A payment held at AUTHORISED is a REVIEW that never reached APPROVED. There is no
  // `PENDING REVIEW` lifecycle state to read (it is in Doina's L530 list but not in the
  // canonical `status` enum — Q33), so the hold is inferred from the two states, which is
  // exactly the ambiguity Q33 asks her to resolve.
  if (decision === "REVIEW" && !reached("APPROVED")) return "held for review";
  if (decision === "DECLINED") return "declined";
  if (decision && network) return `${decision} · ${network}`;
  if (decision) return `fraud ${decision}`;
  if (reached("ROUTED")) return network ? `routed · ${network}` : "routed";
  return "stage 4";
}

function stageFiveMeta(payment, reached, execution, tx) {
  // A book transfer reaches no rail, so "no message" is the honest summary rather than an
  // omission — that is Doina's own "only at the rail boundary" (L542), visible in the rail.
  if (execution) {
    const network = execution.clearingNetwork || payment?.wireDetails?.network;
    return network ? `${execution.messageFormat} · ${network}` : execution.messageFormat;
  }
  if (tx) return `${tx.rail || payment?.rail} · book transfer`;
  if (reached("SUBMITTED")) return "submitted";
  return "stage 5";
}

export function buildLifecycleStages(payment, trace) {
  const tx = trace?.transaction ?? null;
  const le = trace?.ledgerEvent ?? null;
  const sls = trace?.subLedgerEntries ?? [];
  const jn = trace?.journalEntry ?? null;
  const names = trace?.accountNames ?? {};
  const events = payment?.lifecycle?.events ?? [];
  const checks = payment?.checks ?? [];

  // Stage 2's own checks only. Every other stage filters its checks by stage prefix
  // (3/4/5 below); stage 2 historically passed the whole `checks` array, so it rendered
  // every check from every stage under "Authentication & entitlement" and counted them
  // all in the meta. Doina's FR-2.6 is explicit that fraud/risk auth belong to stage 4,
  // not here — so the filter is a correctness fix, not cosmetics.
  const stageTwoChecks = checks.filter((c) => String(c?.stage || "").startsWith("2 "));

  // Stage 6's fee ledger event — a SECOND ledgerEvents doc keyed {paymentId}-FEE
  // (ingest_worker, stage 6). The backend has returned it as `trace.feeEvent` since
  // doc 21 step 8, but it was never rendered, so the fee legs (DR 2111 / CR 4211)
  // were invisible. Surfaced here as its own panel in the accounting group, mirroring
  // the principal ledger event. Absent for internal transfers and no-fee wires.
  const feeEvent = trace?.feeEvent ?? null;

  // Stage 7 — settlement event and position (doc 21 step 8).
  const se = trace?.settlementEvent ?? null;
  const positions = payment?.settlementPositions ?? [];
  const position = positions.length ? positions[0] : null;
  const clearing = payment?.clearing ?? {};

  const reached = (...states) => events.some((e) => states.includes(e.state));
  const eventsFor = (...states) => events.filter((e) => states.includes(e.state));

  // 2026-09-09 (Kiran): a payment HELD at the step-up gate sits at INITIATED with
  // `stepUpRequired` set — stage 2 has evaluated the (insufficient) assertion but recorded
  // no `checks[]` yet, so it must NOT render as "not reached". It is exactly where the
  // analyst must approve: the stage-2 panel shows the required-verification CTA.
  const heldForStepUp =
    payment?.stepUpRequired === true && payment?.status === "INITIATED";

  // Stage 5's artifacts live in their own collections, so `/workflow/payments/{id}` joins
  // them on (doc 19 §3 step 8). The LAST attempt is the current one — the array is
  // append-only, so its order is the history.
  const executions = payment?.executions ?? [];
  const execution = executions.length ? executions[executions.length - 1] : null;
  const message = (payment?.messages ?? []).find(
    (m) => m.paymentMessageId === execution?.paymentMessageId
  ) ?? (payment?.messages ?? [])[0] ?? null;

  return [
    {
      key: "initiation",
      label: "Initiation",
      icon: "Edit",
      stage: 1,
      reached: !!payment,
      status: payment?.status,
      meta: payment?.paymentId,
      kind: "initiation",
      data: payment,
      intro:
        "Captures the payment instruction and creates the canonical payments document " +
        "immediately. Payment type and rail are set from the customer's selection at creation — " +
        "never inferred downstream — and the debtor and creditor are frozen as immutable, " +
        "point-in-time snapshots: the Travel Rule (FATF Rec 16) requires originator details to " +
        "travel unchanged with the payment.",
    },
    {
      key: "authentication",
      label: "Authentication & entitlement",
      icon: "Lock",
      stage: 2,
      reached: stageTwoChecks.length > 0 || heldForStepUp,
      meta: heldForStepUp
        ? "verification required"
        : stageTwoChecks.length
          ? `${stageTwoChecks.length} checks`
          : "stage 2",
      kind: "checks",
      data: stageTwoChecks,
      actionRequired: heldForStepUp,
      intro:
        "Answers one question — is this caller allowed to initiate this amount? Two gates: " +
        "party authentication — is this the real customer, corporate user, or API? — and " +
        "payment entitlement — is this caller allowed to initiate this amount from this " +
        "account? Records the authentication{} and entitlement{} assessments and the checks[] " +
        "results the gates produce, including dual approval once the amount clears the segment " +
        "threshold.",
      raw: {
        authentication: payment?.authentication ?? null,
        entitlement: payment?.entitlement ?? null,
        checks: stageTwoChecks,
      },
    },
    {
      // Stage 3 owns three states (VALIDATED -> ENRICHED -> FINAL_VALIDATED) and two kinds
      // of output: ten-plus `checks[]` entries and the `enrichment{}` before/after record.
      // `kind: "enrichment"` renders the diff — her L459-460 acceptance — alongside the
      // state transitions and the stage's own checks.
      key: "validation",
      label: "Validation & enrichment",
      icon: "Checkmark",
      stage: 3,
      reached: reached("VALIDATED", "ENRICHED", "FINAL_VALIDATED"),
      meta: stageThreeMeta(payment, reached),
      intro:
        "Validates the instructed payment — structure, the debtor and creditor accounts, and " +
        "duplicate and idempotency — then enriches the gaps: bank and clearing-member IDs, " +
        "routing data, a purpose code, regulatory info and FX. Records the domestic/cross-" +
        "border determination, and confirms the chosen payment type is viable on the rail.",
      kind: "enrichment",
      data: {
        events: eventsFor("VALIDATED", "ENRICHED", "FINAL_VALIDATED"),
        enrichment: payment?.enrichment || null,
        // Every check any stage-3 half recorded. Empty-tolerant: a payment written before
        // stage 3 existed simply has none.
        checks: (payment?.checks || []).filter((c) =>
          String(c?.stage || "").startsWith("3 ")
        ),
      },
    },
    {
      // Stage 4 owns three states (ROUTED -> AUTHORISED -> APPROVED) and four kinds of
      // output: the routing decision, the fraud assessment, the sanctions result and the
      // authorization decision. `kind: "authorization"` renders Doina's L508-515 display —
      // "Risk assessment completed", "Sanctions and AML screening passed", "Fraud risk score
      // within threshold", "Decision: APPROVED" — straight off `checks[]`, which is why the
      // backend check NAMES match her four lines one-for-one.
      key: "authorization",
      label: "Orchestration & authorization",
      icon: "Diagram3",
      stage: 4,
      reached: reached("ROUTED", "AUTHORISED", "APPROVED"),
      meta: stageFourMeta(payment, reached),
      intro:
        "Chooses the execution path within the already-selected rail, writes the immutable " +
        "routing snapshot, and confirms the commitment back to the originator. Then scores the " +
        "fully-formed payment for fraud and runs transaction-level authorization — approve, " +
        "decline, or hold.",
      kind: "authorization",
      data: {
        events: eventsFor("ROUTED", "AUTHORISED", "APPROVED"),
        fraud: payment?.fraud || null,
        sanctions: payment?.correspondent?.sanctionsCheck || null,
        network: payment?.wireDetails?.network || null,
        // Forward pointers, not the documents themselves: `routingSnapshots` and
        // `paymentOrders` are separate collections, and the deep-dive endpoint returns the
        // payment only. Showing the refs proves the artifacts exist and gives an operator
        // the ids to look them up with, without adding a second fetch to this panel.
        refs: {
          routingSnapshotId: payment?.refs?.routingSnapshotId || null,
          paymentOrderId: payment?.refs?.paymentOrderId || null,
        },
        checks: (payment?.checks || []).filter((c) =>
          String(c?.stage || "").startsWith("4 ")
        ),
      },
    },
    {
      // Stage 5 owns two states (SUBMITTED -> IN_PROGRESS) and two artifacts: the pacs.008
      // on `paymentExecutions` and the canonical payload on `paymentMessages`.
      // `kind: "railExecution"` renders her L548-565 BUSINESS VIEW / ISO VIEW pair — doc 16
      // §5 calls it the highest-value screen in the demo.
      //
      // ⚠️ `reached` used to be `!!tx` — the ledger trace's transaction. That made an
      // external wire show stage 5 as never reached even though it had been submitted and
      // acknowledged, because an external creditor produces no `transactions` doc by design
      // (doc 19 B1). Read the lifecycle first and fall back to the transaction, so both a
      // book transfer (no artifacts, has a tx) and an external wire (artifacts, no tx)
      // register.
      key: "execution",
      label: "Rail execution",
      icon: "Beaker",
      stage: 5,
      reached: reached("SUBMITTED", "IN_PROGRESS") || !!tx,
      status: execution?.status ?? tx?.transactionStatus,
      meta: stageFiveMeta(payment, reached, execution, tx),
      intro:
        "Transforms the canonical payment into a rail-specific message at the rail boundary — a " +
        "pacs.008 for a wire — submits it to the network, and records the execution and its " +
        "acknowledgement. A book transfer reaches no rail and is recorded as exactly that.",
      kind: "railExecution",
      data: {
        events: eventsFor("SUBMITTED", "IN_PROGRESS"),
        execution,
        attempts: executions,
        // BUSINESS VIEW (her L550-553) and ISO VIEW (L555-563) — the two tabs, from the two
        // documents. `business` falls back to the payment itself so a pre-stage-5 payment
        // still renders something rather than an empty tab.
        business: message?.payload ?? null,
        iso: execution?.message ?? null,
        // Derived server-side (stdlib ElementTree) and returned by the same route — the UI
        // never serialises XML itself.
        xml: execution?.messageXml ?? null,
        transformationAudit: message?.transformationAudit ?? [],
        mappingVersion: message?.mappingVersion ?? null,
        clearing: payment?.clearing ?? null,
        railStatus: execution?.railStatus ?? null,
        simulated: execution?.simulated ?? false,
        transaction: tx,
        checks: checks.filter((c) => String(c?.stage || "").startsWith("5 ")),
      },
    },
    {
      key: "ledgerEvent",
      label: "Ledger event",
      icon: "Copy",
      stage: 6,
      group: "Accounting & posting",
      reached: !!le,
      status: le?.postingStatus,
      // The payment's own posting fact, not the event's postingMode (which was always
      // "BATCH" — a constant, so it told the reader nothing).
      meta: accountingMeta(payment, jn),
      intro:
        "Posts the balanced debit and credit legs at minor-unit precision — the payment's own " +
        "accounting fact, written by the ledger service — and captures the financial history as " +
        "sub-ledger entries that roll up into a journal entry, whose id is written back to the " +
        "preceding records.",
      kind: "ledgerEvent",
      data: le,
      legs: le
        ? {
            currency: le.debitLeg?.currency || le.creditLeg?.currency || "USD",
            debits: le.debitLeg
              ? [leg(le.debitLeg.glAccountCode, names[le.debitLeg.glAccountCode] || "", le.debitLeg.amount)]
              : [],
            credits: le.creditLeg
              ? [leg(le.creditLeg.glAccountCode, names[le.creditLeg.glAccountCode] || "", le.creditLeg.amount)]
              : [],
          }
        : null,
    },
    {
      // The fee is a second ledgerEvents doc (idempotencyKey {paymentId}-FEE), not a
      // second leg of the principal. It has its own debit/credit legs, its own subledger
      // rows, and its own journal entry — so it gets its own panel in the accounting
      // group, reusing the `ledgerEvent` renderer. Reached only when a fee was levied
      // (wires with a stage-3 charge); internal transfers and no-fee wires have none.
      key: "feeLedgerEvent",
      label: "Fee ledger event",
      icon: "Copy",
      stage: 6,
      group: "Accounting & posting",
      reached: !!feeEvent,
      status: feeEvent?.postingStatus,
      meta: feeEvent
        ? (feeEvent.postingResult?.journalEntryId || "wire fee")
        : null,
      intro:
        "The wire fee is a second balanced event — its own debit and credit legs, its own " +
        "sub-ledger rows and its own journal entry — not a second leg of the principal. Present " +
        "only when stage 3 levies a charge on a wire.",
      kind: "ledgerEvent",
      data: feeEvent,
      legs: feeEvent
        ? {
            currency: feeEvent.debitLeg?.currency || feeEvent.creditLeg?.currency || "USD",
            debits: feeEvent.debitLeg
              ? [leg(feeEvent.debitLeg.glAccountCode, names[feeEvent.debitLeg.glAccountCode] || "", feeEvent.debitLeg.amount)]
              : [],
            credits: feeEvent.creditLeg
              ? [leg(feeEvent.creditLeg.glAccountCode, names[feeEvent.creditLeg.glAccountCode] || "", feeEvent.creditLeg.amount)]
              : [],
          }
        : null,
    },
    {
      key: "subLedger",
      label: "Sub-ledger",
      icon: "List",
      stage: 6,
      group: "Accounting & posting",
      reached: sls.length > 0,
      status: sls.length ? (sls.every((e) => e.journalEntryId) ? "POSTED" : "PENDING") : null,
      meta: sls.length ? `${sls.length} entries` : null,
      intro:
        "The paired control-account entry for each side of a posting — one debit, one credit, " +
        "balanced — stamped with the journal-entry id once the batch posts. The general ledger " +
        "aggregates these.",
      kind: "subLedger",
      data: sls,
      legs: sls.length
        ? {
            currency: sls[0]?.currency || "USD",
            debits: sls.filter((e) => e.side === "DEBIT")
              .map((e) => leg(e.controlAccountCode, names[e.controlAccountCode] || e.subLedgerType || "", e.amount)),
            credits: sls.filter((e) => e.side === "CREDIT")
              .map((e) => leg(e.controlAccountCode, names[e.controlAccountCode] || e.subLedgerType || "", e.amount)),
          }
        : null,
    },
    {
      key: "generalLedger",
      label: "General ledger",
      icon: "Building",
      stage: 6,
      group: "Accounting & posting",
      reached: !!jn,
      status: jn?.status,
      meta: jn?.periodCode,
      intro:
        "The aggregation of the sub-ledger entries by (period, control account, side) into a " +
        "posted journal entry — the moment the accounting facts become a balanced, immutable " +
        "journal.",
      kind: "journal",
      data: jn,
      legs: jn
        ? {
            currency: jn.currency || "USD",
            debits: (jn.entries || []).filter((e) => e.side === "DEBIT")
              .map((e) => leg(e.accountCode, names[e.accountCode] || e.accountName || "", e.amount)),
            credits: (jn.entries || []).filter((e) => e.side === "CREDIT")
              .map((e) => leg(e.accountCode, names[e.accountCode] || e.accountName || "", e.amount)),
          }
        : null,
    },
    {
      key: "settlement",
      label: "Clearing & settlement",
      icon: "ArrowLeftRight",
      stage: 7,
      reached: reached("SETTLED", "FAILED", "RETURNED") || !!position,
      status: payment?.lifecycle?.settlementStatus || undefined,
      meta: position?.modelLabel || (clearing.settledAt ? "settled" : "pending"),
      intro:
        "Posts the credit to a clearing or correspondent account, simulates the external " +
        "settlement response, and distinguishes internal posting from external settlement. " +
        "Records the settlement position and the settlement event — SETTLED no longer happens " +
        "inside the money move.",
      kind: "legs",
      data: { position, clearing },
      legs: se
        ? {
            currency: se.creditLeg?.currency || "USD",
            debits: [leg(se.debitLeg?.glAccountCode, names[se.debitLeg?.glAccountCode] || se.debitLeg?.entityReference?.entityId || "", se.debitLeg?.amount)],
            credits: [leg(se.creditLeg?.glAccountCode, names[se.creditLeg?.glAccountCode] || "Settlement account", se.creditLeg?.amount)],
          }
        : null,
    },
    {
      // Stage 8 — three-way reconciliation (doc 22). The five-row tie-out reads
      // `trace.reconciliation` (the ledger's three-leg result) plus the settlementPosition the
      // legs were checked against. `reached` is true once the check has run at all — a PENDING
      // check still counts as reached, so the panel renders "awaiting the GL batch" rather than
      // "not reached".
      key: "reconciliation",
      label: "Reconciliation",
      icon: "Checkmark",
      stage: 8,
      reached: reached("RECONCILED") || !!trace?.reconciliation,
      status: trace?.reconciliation?.overallResult || undefined,
      meta: trace?.reconciliation
        ? (trace.reconciliation.overallResult === "RECONCILED"
            ? "reconciled"
            : trace.reconciliation.overallResult === "DISCREPANT"
              ? "discrepancy"
              : "awaiting the GL batch")
        : (reached("RECONCILED") ? "reconciled" : "stage 8"),
      intro:
        "Runs the three-way match — payment to rail, rail to settlement account, settlement " +
        "account to the general ledger — and flags any discrepancy. Runs in the ledger service " +
        "after the settlement journal posts, so RECONCILED arrives asynchronously.",
      kind: "reconciliation",
      data: {
        check: trace?.reconciliation ?? null,
        events: eventsFor("RECONCILED"),
        position,
      },
    },
  ];
}

export const legTotals = (legs) => {
  const debit = sum(legs.debits);
  const credit = sum(legs.credits);
  return { debit, credit, balanced: debit === credit };
};
