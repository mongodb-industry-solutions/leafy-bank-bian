/**
 * The nine-stage lifecycle, as presentation stages for the horizontal rail.
 *
 * Composes the two halves of a payment's trace into ONE left-to-right saga:
 *   * stages 1-4 from /workflow/payments/{id}   (transactions service — payments doc)
 *   * stages 5-8 from /pipeline/trace/{id}      (ledger service)
 * Neither service reads the other's collections; the join happens here.
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

  const reached = (...states) => events.some((e) => states.includes(e.state));
  const eventsFor = (...states) => events.filter((e) => states.includes(e.state));

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
    },
    {
      key: "authentication",
      label: "Authentication & entitlement",
      icon: "Lock",
      stage: 2,
      reached: checks.length > 0,
      meta: checks.length ? `${checks.length} checks` : "stage 2",
      kind: "checks",
      data: checks,
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
      reached: !!le,
      status: le?.postingStatus,
      meta: le?.postingMode?.type,
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
      key: "subLedger",
      label: "Sub-ledger",
      icon: "List",
      stage: 7,
      reached: sls.length > 0,
      status: sls.length ? (sls.every((e) => e.journalEntryId) ? "POSTED" : "PENDING") : null,
      meta: sls.length ? `${sls.length} entries` : null,
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
      stage: 8,
      reached: !!jn,
      status: jn?.status,
      meta: jn?.periodCode,
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
      key: "reconciliation",
      label: "Reconciliation",
      icon: "Checkmark",
      stage: 9,
      reached: reached("RECONCILED"),
      meta: reached("RECONCILED") ? "reconciled" : "stage 9",
      kind: "states",
      data: eventsFor("RECONCILED"),
    },
  ];
}

export const legTotals = (legs) => {
  const debit = sum(legs.debits);
  const credit = sum(legs.credits);
  return { debit, credit, balanced: debit === credit };
};
