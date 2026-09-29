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
function accountingMeta(payment, jn, le) {
  const posting = payment?.lifecycle?.postingStatus;
  const journalRef = payment?.refs?.journalEntryId;
  if (posting === "POSTED" && journalRef) return journalRef;
  if (le) return "captured — awaiting the GL batch";
  if (jn) return jn.periodCode;
  // An external wire writes a `transactions` doc (payee = the clearing account) and reaches
  // the ledger, but settlement is deferred to stage 7 — so before settle.py runs the payment
  // is at IN_PROGRESS and the GL batch has not posted it yet. Say that, rather than rendering
  // an empty panel that reads like a bug. (Pre-stage-7 this was "writes no transactions doc,
  // reaches no ledgerEvent" — stale after doc 21 step 3 removed the halt.)
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

  // A REVIEW decision holds at MANUAL_FRAUD_REVIEW (FR-4.13 / Q33 resolved 2026-09-11) — its own
  // status now, read directly rather than inferred from "AUTHORISED but not APPROVED".
  if (payment?.lifecycle?.currentState === "MANUAL_FRAUD_REVIEW") return "manual fraud review";
  // Fallback for docs written before the state existed (a REVIEW held at AUTHORISED).
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

/**
 * Whether `payment` is an inbound wire. Stages 1-5 read differently for inbound (Doina's
 * Sep 17 doc, "Incoming — Stage Summary" table, L1591-1602 — labels below are her exact
 * column-1 wording, not paraphrases): stage 1 is passive receipt rather than customer
 * submission, stage 2 becomes beneficiary resolution instead of caller authentication,
 * stage 3 drops routing/fee enrichment and adds originator sanctions screening, stage 4 has
 * no routing decision (it's a binary accept/reject), and stage 5 confirms receipt (a
 * pacs.002) rather than submitting a payment. Stages 6-8 are the same shape both ways —
 * her own framing is "only the direction of the debit/credit legs" (L1039) — so no branch
 * needed there. Stage 9 (UTA) exists in her doc but is out of scope for now (2026-09-29,
 * Kiran) — not built into this rail.
 */
const isInbound = (payment) => payment?.direction === "INBOUND";

export function buildLifecycleStages(payment, trace) {
  const inbound = isInbound(payment);
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
  // A fee is levied only on a wire (enrichment _plan_fees SKIPs rail != WIRE), so the
  // fee ledger-event stage is structurally empty for internal transfers and no-fee
  // wires. But a recorded fee is NOT the same as a posted one: `_debtor_borne_fee`
  // (stage 5) carries only a DEBTOR/SHARED (or SLEV→DEBTOR) charge to the ledger and
  // skips a CREDITOR-borne one (Q44) — which `chargeBearer: CRED` produces. Key the
  // hide on whether the ledger will actually post a fee leg, not on `fees` being
  // non-empty, or a creditor-borne wire shows an empty "Fee ledger event" panel that
  // will never fill. A fee that IS debtor-borne may briefly show feeEvent null before
  // the worker ingests it — that transient should still render as "pending", not vanish.
  const hasFee = (payment?.fees ?? []).some((f) =>
    ["DEBTOR", "SHARED"].includes((f ?? {}).chargedTo)
  );

  // Stage 7 — settlement event and position (doc 21 step 8).
  const se = trace?.settlementEvent ?? null;
  const positions = payment?.settlementPositions ?? [];
  const position = positions.length ? positions[0] : null;
  const clearing = payment?.clearing ?? {};

  const reached = (...states) => events.some((e) => states.includes(e.state));
  const eventsFor = (...states) => events.filter((e) => states.includes(e.state));

  // The payment's exception occurrences (joined by `get_payment`, doc 24 §3 step 8).
  // Rather than a separate stage-9 panel, each exception is routed to the stage that
  // produced it — its `source.stage` carries the originating stage number (e.g.
  // "7 settle", "8 reconcile", "3 validate"). Doina's own framing supports this:
  // exceptions have no BIAN service domain and are "modeled within the originating
  // domain", so the remedy belongs at the failure site, not a peer timeline node.
  const exceptions = payment?.exceptions || [];
  // Group exceptions by their originating stage number, so each stage node carries
  // only the exceptions it raised. Keyed by the leading integer of `source.stage`.
  const exceptionsByStage = {};
  for (const e of exceptions) {
    const n = parseInt(String(e?.source?.stage || ""), 10);
    if (Number.isInteger(n)) {
      (exceptionsByStage[n] ||= []).push(e);
    }
  }
  // Which stage numbers have already had their exceptions attached to a panel. A stage with
  // several panels (stage 7: External settlement + Settlement posting) must render the
  // resolve CTAs exactly once — see the `.map` below.
  const claimedStages = new Set();

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
      label: inbound ? "Payment Order Initiation (Inbound)" : "Initiation",
      icon: "Edit",
      stage: 1,
      reached: !!payment,
      status: payment?.status,
      meta: payment?.paymentId,
      kind: "initiation",
      data: payment,
      intro: inbound
        ? "A wire arrives from the sending bank as an interbank message rather than a " +
          "customer submission — there is no capture screen and no rail to choose; the " +
          "channel it arrived on already fixes that. The original message is preserved as " +
          "evidence first, then the payment record is created from it. The beneficiary named " +
          "in the message is only a claim at this point — Leafy Bank confirms it owns that " +
          "account next, at stage 2."
        : "Captures the payment instruction and creates the canonical payments document " +
          "immediately. Payment type and rail are set from the customer's selection at creation — " +
          "never inferred downstream — and the debtor and creditor are frozen as immutable, " +
          "point-in-time snapshots: the Travel Rule (FATF Rec 16) requires originator details to " +
          "travel unchanged with the payment.",
    },
    {
      key: "authentication",
      label: inbound ? "Message Authentication & Beneficiary Resolution" : "Authentication & entitlement",
      icon: "Lock",
      stage: 2,
      reached: inbound
        ? !!payment?.beneficiaryResolution
        : stageTwoChecks.length > 0 || heldForStepUp,
      meta: inbound
        ? (payment?.beneficiaryResolution?.matchOutcome
            ? payment.beneficiaryResolution.matchOutcome
            : "stage 2")
        : heldForStepUp
          ? "verification required"
          : stageTwoChecks.length
            ? `${stageTwoChecks.length} checks`
            : "stage 2",
      kind: inbound ? "beneficiaryResolution" : "checks",
      data: inbound ? payment?.beneficiaryResolution ?? null : stageTwoChecks,
      actionRequired: heldForStepUp,
      // A different question entirely for inbound (Doina L488): not "is this caller allowed
      // to send this amount?" but "is this a legitimate message, and does the account it
      // names actually belong to the person it claims to belong to?" There is no customer to
      // authenticate — no step-up, no entitlement policy — so this stage instead resolves the
      // CLAIMED creditor from stage 1 against Leafy Bank's own account records.
      intro: inbound
        ? "A different question from outbound's stage 2 — there is no customer to " +
          "authenticate here, only a sending bank. First the message itself is authenticated " +
          "at the network level, then the claimed beneficiary from stage 1 is checked " +
          "against Leafy Bank's own account records: does the account exist and is it open, " +
          "and does the name on the wire match the name on file? A full match proceeds; a " +
          "close-but-not-exact name proceeds with a flag; no match at all sends the payment " +
          "straight to Exceptions rather than continuing forward."
        : "Answers one question — is this caller allowed to initiate this amount? Two gates: " +
          "party authentication — is this the real customer, corporate user, or API? — and " +
          "payment entitlement — is this caller allowed to initiate this amount from this " +
          "account? Records both assessments and their results, including dual approval once " +
          "the amount clears the segment threshold.",
      raw: inbound
        ? { beneficiaryResolution: payment?.beneficiaryResolution ?? null }
        : {
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
      // Same three states both ways — RECEIVED and INITIATED both forward into VALIDATED
      // (lifecycle.py's `_FORWARD`), so this stage's reached-check needs no direction branch.
      reached: reached("VALIDATED", "ENRICHED", "FINAL_VALIDATED"),
      meta: stageThreeMeta(payment, reached),
      // Her L699: "structural/account/beneficiary validation already happened in Stage 2 for
      // inbound... the sequence is reordered relative to outgoing, not duplicated." What's
      // new for inbound is sanctions/AML screening of the ORIGINATOR (the mirror of
      // outbound's stage-4 screening of the counterparty) and incoming FX — most of
      // outbound's enrichment (routing data, clearing-member IDs, fee estimation) does not
      // apply, because routing already happened on the sender's side.
      intro: inbound
        ? "Screens the sending party for sanctions before any money is credited at stage 6 " +
          "— the mirror of outbound's counterparty screening, run earlier here because " +
          "accepting funds is itself a compliance decision, not just releasing them. If the " +
          "payment arrives in a different currency than the beneficiary's account, a " +
          "simulated exchange rate converts it, and the payment is classified domestic or " +
          "cross-border. Routing and fee work do not apply here — that already happened on " +
          "the sender's side before the message reached Leafy Bank."
        : "Validates the instructed payment — structure, the debtor and creditor accounts, and " +
          "duplicate and idempotency — then enriches the gaps: bank and clearing-member IDs, " +
          "routing data, a purpose code, regulatory info and FX. Records the domestic/cross-" +
          "border determination, and confirms the chosen payment type is viable on the rail.",
      kind: "enrichment",
      data: {
        events: eventsFor("VALIDATED", "ENRICHED", "FINAL_VALIDATED"),
        enrichment: payment?.enrichment || null,
        // Inbound writes sanctions + fx directly rather than through the enrichment{} diff
        // (screen_and_accept.py sets `correspondent.sanctionsCheck` / `fx`, not
        // `enrichment.resolved`) — surfaced here so the panel isn't empty for inbound.
        originatorSanctionsCheck: inbound ? payment?.correspondent?.sanctionsCheck ?? null : null,
        fx: inbound ? payment?.fx ?? null : null,
        // Every check any stage-3 half recorded. Empty-tolerant: a payment written before
        // stage 3 existed simply has none.
        checks: (payment?.checks || []).filter((c) =>
          String(c?.stage || "").startsWith("3 ")
        ),
      },
    },
    {
      // Stage 4 owns ROUTED -> (MANUAL_FRAUD_REVIEW | AUTHORISED) -> APPROVED and four kinds of
      // output: the routing decision, the fraud assessment, the sanctions result and the
      // authorization decision. `kind: "authorization"` renders Doina's L508-515 display —
      // "Risk assessment completed", "Sanctions and AML screening passed", "Fraud risk score
      // within threshold", "Decision: APPROVED" — straight off `checks[]`, which is why the
      // backend check NAMES match her four lines one-for-one. MANUAL_FRAUD_REVIEW is the REVIEW
      // hold (FR-4.13): the payment is routed but not yet authorised.
      key: "authorization",
      label: inbound ? "Acceptance Decision & Compliance Authorization" : "Orchestration & authorization",
      icon: "Diagram3",
      stage: 4,
      // Inbound has no ROUTED/AUTHORISED/APPROVED — FINAL_VALIDATED forwards straight to
      // ACCEPTED (lifecycle.py `_FORWARD[FINAL_VALIDATED] = {ROUTED, ACCEPTED}`).
      reached: inbound
        ? !!payment?.acceptanceDecision
        : reached("ROUTED", "MANUAL_FRAUD_REVIEW", "AUTHORISED", "APPROVED"),
      meta: inbound
        ? (payment?.acceptanceDecision?.decision || "stage 4")
        : stageFourMeta(payment, reached),
      // Her L823: "there is no execution-path or rail-selection decision... the routing
      // already happened before the message reached Leafy Bank." What replaces outbound's
      // orchestration + fraud scoring is a binary ACCEPT/REJECT rollup of stage 2's
      // beneficiary match and stage 3's sanctions outcome (FR-4.IN1). On REJECT the payment
      // routes to Exceptions rather than advancing (FR-4.IN3) — never held for manual
      // review, which is an outbound-only concept (FR-4.13) with no inbound equivalent.
      intro: inbound
        ? "No routing choice to make here — the sending bank already chose the path before " +
          "the message arrived. Instead, this stage rolls up the two checks that came " +
          "before it — did the beneficiary match, did the originator clear sanctions — into " +
          "one decision: accept the payment, or reject it back to the sender. Accepting " +
          "leads to a confirmation being sent at stage 5; rejecting stops the payment here " +
          "and routes it to Exceptions instead."
        : "Chooses the execution path within the already-selected rail, writes the immutable " +
          "routing snapshot, and confirms the commitment back to the originator. Then scores the " +
          "fully-formed payment for fraud and runs transaction-level authorization — approve, " +
          "decline, or hold.",
      kind: inbound ? "acceptanceDecision" : "authorization",
      data: inbound
        ? {
            events: eventsFor("ACCEPTED"),
            acceptanceDecision: payment?.acceptanceDecision || null,
            checks: (payment?.checks || []).filter((c) =>
              String(c?.stage || "").startsWith("4 ")
            ),
          }
        : {
        events: eventsFor("ROUTED", "MANUAL_FRAUD_REVIEW", "AUTHORISED", "APPROVED"),
        fraud: payment?.fraud || null,
        sanctions: payment?.correspondent?.sanctionsCheck || null,
        network: payment?.wireDetails?.network || null,
        // FR-4.13 — an operator can resolve a payment held here for manual review. The
        // resolve buttons render in the stage-4 panel only while the payment is actually at
        // MANUAL_FRAUD_REVIEW; once resolved (APPROVED → IN_PROGRESS, or REJECTED) this is false.
        reviewActionRequired: payment?.lifecycle?.currentState === "MANUAL_FRAUD_REVIEW",
        // FR-4.1 — the execution-strategy decision (strategy, network, correspondent,
        // cost, cut-off, value date, rationale). Joined onto the payment read from
        // `routingSnapshots` (workflow_read_service.get_payment). The payment doc itself
        // carries only `wireDetails.network` + the snapshot id; the full decision lives on
        // the immutable snapshot, so it is surfaced here for the stage-4 panel to render.
        routingSnapshot: payment?.routingSnapshot || null,
        // Forward pointers to stage 4's artifacts. `routingSnapshots` is a separate
        // collection; the commitment was folded into `payments.order` per Doina's Aug 27
        // target model (L427-429), so `paymentOrderId` now points within the same document
        // to `order.paymentOrderId`. Showing the refs proves the artifacts exist and gives
        // an operator the ids to look them up with.
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
      label: inbound ? "Execution (Status Response)" : "Rail execution",
      icon: "Beaker",
      stage: 5,
      reached: inbound
        ? reached("ACCEPTED")
        : reached("SUBMITTED", "IN_PROGRESS") || !!tx,
      status: inbound ? undefined : (execution?.status ?? tx?.transactionStatus),
      meta: inbound
        ? (payment?.refs?.statusResponseMessageId ? "pacs.002 ACCP" : "stage 5")
        : stageFiveMeta(payment, reached, execution, tx),
      // Her L938: outbound "executes" by transforming the canonical payment into an
      // outbound rail message and submitting it to a network. Inbound has nothing left to
      // submit — the money already arrived. "Execution" here means generating and
      // transmitting the status response back to the SENDING bank: a simulated pacs.002
      // confirming the payment will be applied. No `paymentExecutions` record is created
      // for inbound (that collection is outbound-only, L944) — the pacs.002 lives in
      // `paymentMessages` alongside outbound's pacs.008, distinguished by `direction`.
      intro: inbound
        ? "The money already arrived, so there's nothing left to submit to a network — " +
          "\"execution\" here means telling the sending bank what happened. A confirmation " +
          "message goes back: accepted if stage 4 approved it, rejected if it did not. " +
          "Sending that confirmation is what advances the payment forward, ahead of the " +
          "internal posting that follows at stage 6."
        : "Transforms the canonical payment into a rail-specific message at the rail boundary — a " +
          "pacs.008 for a wire — submits it to the network, and records the execution and its " +
          "acknowledgement. A book transfer reaches no rail and is recorded as exactly that.",
      kind: "railExecution",
      data: {
        events: inbound ? eventsFor("ACCEPTED") : eventsFor("SUBMITTED", "IN_PROGRESS"),
        execution,
        attempts: executions,
        // BUSINESS VIEW (her L550-553) and ISO VIEW (L555-563) — the two tabs. Outbound's
        // business view is the pacs.008 payload (`message.payload`, shaped by
        // `execution_documents.canonical_payload`). Inbound's `message` is the pacs.002
        // status response instead — its payload has NO debtor/creditor/amount fields (it is
        // a status report, not a payment instruction), so the business view is built here
        // from the payment doc itself, which already carries every field the row list reads.
        business: inbound
          ? {
              paymentId: payment?.paymentId,
              debtorName: payment?.debtor?.name,
              creditorName: payment?.creditor?.name,
              amount: payment?.amount,
              currency: payment?.currency,
              rail: payment?.rail,
              clearingNetwork: payment?.wireDetails?.network,
              endToEndId: payment?.senderReferences?.endToEndId,
              uetr: payment?.uetr,
              chargeBearer: payment?.chargeBearer,
              creditorBankName: payment?.creditor?.bankName,
              creditorBankCountry: payment?.creditor?.bankCountry,
              creditorBic: payment?.creditor?.bic,
              purposeCode: payment?.categoryPurpose,
              remittanceInfo: payment?.remittance?.unstructured,
            }
          : message?.payload ?? null,
        iso: inbound ? message?.payload ?? null : execution?.message ?? null,
        // Derived server-side (stdlib ElementTree) and returned by the same route — the UI
        // never serialises XML itself. Inbound has no execution doc to carry one.
        xml: inbound ? null : execution?.messageXml ?? null,
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
      // "COMPLETED" (not le.postingStatus) so the panel goes green once the balanced event is
      // CAPTURED — seconds after execution via CDC. le.postingStatus flips to POSTED only at
      // the GL batch (10 min); keying the panel on it left the event "stuck on pending" for the
      // whole batch window. The GL-post detail (journalEntryId, postedAt) still shows in the
      // detail rows, so the batch wait remains visible — just no longer gating the green mark.
      status: le ? "COMPLETED" : null,
      // The payment's own posting fact, not the event's postingMode (which was always
      // "BATCH" — a constant, so it told the reader nothing).
      meta: accountingMeta(payment, jn, le),
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
      // Same rationale as the principal ledger event: green when captured, not when the GL
      // batch journals it.
      status: feeEvent ? "COMPLETED" : null,
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
      // "COMPLETED" when the paired entries exist (seconds, via the projection worker) — not
      // gated on journalEntryId, which is stamped only at the GL batch. The batch wait is the
      // General-ledger sub-panel's job, not this one.
      status: sls.length ? "COMPLETED" : null,
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
    // Stage 7 — Clearing & settlement, split into two sub-steps so the rail shows the
    // progression Doina's A.1 describes: external settlement is CONFIRMED first (independent
    // of the GL batch), then the second accounting event is POSTED to the GL by the batch.
    // The split makes the "external settlement has gone green, and only after the GL batch
    // runs does the posting finally go green" story visible — instead of one pill that can't
    // distinguish "confirmed" from "posted".
    {
      // Sub-step 7a: external settlement confirmation. settlementStatus PENDING -> SETTLED at
      // the deferred window (~30s), independent of the GL batch. The position records the
      // four-way outcome (FR-7.3) for stage-8 reconciliation.
      key: "settlementConfirm",
      label: "External settlement",
      icon: "ArrowLeftRight",
      stage: 7,
      group: "Clearing & settlement",
      reached: !!position || !!payment?.lifecycle?.settlementStatus,
      status: payment?.lifecycle?.settlementStatus || undefined,
      meta: position?.modelLabel || (clearing.settledAt ? "settled" : "pending"),
      intro:
        "Simulates the external settlement response and confirms the outcome — matched, " +
        "unmatched, delayed, or exception. The settlement status advances here, independently " +
        "of the GL batch: once external settlement is confirmed, the second accounting event " +
        "posts.",
      kind: "legs",
      data: { position, clearing, event: se },
    },
    {
      // Sub-step 7b: the settlement accounting event (Dr Wire Clearing / Cr Nostro or Central
      // Bank) and its posting to the GL. The event is created on the SETTLED flip via CDC, but
      // its postingStatus flips to POSTED only when the GL batch journals it — so this sub-step
      // is the one that goes green last, after the batch.
      key: "settlementPosting",
      label: "Settlement posting",
      icon: "Copy",
      stage: 7,
      group: "Clearing & settlement",
      reached: !!se,
      status: se?.postingStatus,
      meta: se?.postingResult?.journalEntryId || (se ? "pending journal" : null),
      intro:
        "The second, distinct accounting event — Dr Wire Clearing / Cr Nostro or " +
        "Central Bank — posted once external settlement is confirmed. It only turns green " +
        "once the batch journals it, so this step completes after the External settlement " +
        "step above it.",
      kind: "ledgerEvent",
      data: se,
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
  ]
    // Drop the fee panel BEFORE the exception routing below — a panel that is filtered out
    // must not be the one that claims its stage's exceptions, or they render nowhere.
    .filter((s) => s.key !== "feeLedgerEvent" || hasFee)
    // Route each exception to its originating stage so the resolve CTAs render at the
    // failure site rather than a separate stage-9 panel. `exceptionsByStage` is keyed by
    // the leading integer of the exception's `source.stage`. A RETURN_FUNDS resolution
    // also carries the reversal ledger event (trace.reversalEvent) so the panel can show
    // "compensating movement posted" once the GL batch journals it.
    //
    // A stage number can own MORE than one panel (stage 7 is split into External settlement
    // + Settlement posting). Only the FIRST panel of a stage claims that stage's exceptions:
    // attaching them to every panel with the number rendered the whole ExceptionsPanel twice,
    // giving the operator two live "Return funds" buttons for one exception (the second
    // resolve then 409s on the OPEN guard and surfaces as a red banner on a correct action).
    .map((s) => {
      const ex = claimedStages.has(s.stage) ? [] : (exceptionsByStage[s.stage] || []);
      if (ex.length) claimedStages.add(s.stage);
      const hasReturn = ex.some((e) => e?.resolution?.action === "RETURN_FUNDS");
      const rev = hasReturn ? (trace?.reversalEvent ?? null) : null;
      // Build the reversal's double-entry legs (Dr 1131 clearing / Cr customer deposit)
      // so the ExceptionsPanel can render the compensating movement in detail — the visual
      // confirmation the posting landed. Same shape as the fee event's `legs`.
      const reversalLegs = rev && rev.debitLeg && rev.creditLeg
        ? {
            currency: rev.debitLeg.currency || rev.creditLeg.currency || "USD",
            debits: [leg(rev.debitLeg.glAccountCode, names[rev.debitLeg.glAccountCode] || "", rev.debitLeg.amount)],
            credits: [leg(rev.creditLeg.glAccountCode, names[rev.creditLeg.glAccountCode] || "", rev.creditLeg.amount)],
          }
        : null;
      return { ...s, exceptions: ex, reversalEvent: rev, reversalLegs };
    });
}

/**
 * Presentational grouping of `buildLifecycleStages` output: collapse every panel that shares a
 * `group` into ONE rail node, so the lifecycle reads as Doina's eight stages — not as ~12 nodes
 * with stage 6 fragmented across "Ledger event" / "Sub-ledger" / "General ledger".
 *
 * Stages 1-5, 7, 8 own a single panel (no `group`), so each passes through as its own node.
 * Stage 6's four accounting panels share `group: "Accounting & posting"` and merge into one
 * node whose expanded body stacks the four. `reached` / `status` / `meta` are lifted off the
 * most-informative child so the node's state (nodeStates, stage >= 6 = independent axis) still
 * reflects the axis's OWN terminal fact: the accounting group completes when the general-ledger
 * journal posts, not when any one panel fills.
 *
 * A later multi-panel stage (e.g. stage 9 Exceptions) just gives its panels a shared `group` —
 * this function needs no change. Kept as a pure function so the rail, the timeline and the
 * grouping stay testable without a component tree.
 */
export function groupLifecycleStages(stages) {
  if (!stages?.length) return [];
  const groups = [];       // { key, label, stage, group?, children[] }
  const byLabel = new Map();
  for (const s of stages) {
    const label = s.group || s.label;
    if (byLabel.has(label)) {
      groups[byLabel.get(label)].children.push(s);
    } else {
      byLabel.set(label, groups.length);
      groups.push({
        key: s.group ? `g:${label}` : s.key,
        label,
        stage: s.stage,
        group: s.group,
        children: [s],
      });
    }
  }
  return groups.map((g) => {
    // A single-panel stage is the group: return it unchanged, so the existing single-detail
    // render path treats it exactly as before (its own key/label/status/reached survive).
    if (g.children.length === 1) return g.children[0];
    const children = g.children;
    const anyReached = children.some((c) => c.reached);
    const reachedCount = children.filter((c) => c.reached).length;
    // The group's terminal fact is its LAST NON-JOURNAL child's status: the sub-ledger for
    // stage 6 (the journal is a trailing batch step that must NOT gate the group's green mark),
    // the settlement posting for stage 7. Stage 6's "Accounting & posting" is complete once the
    // balanced event and sub-ledger entries are captured (seconds, via CDC) — the general-ledger
    // journal aggregation is a downstream batch artifact shown in its own sub-panel, not the
    // gate on the stage's ✓. nodeStates keys off this, exactly as the other independent axes do.
    const nonJournal = children.filter((c) => c.kind !== "journal");
    const terminal = nonJournal[nonJournal.length - 1] ?? children[children.length - 1];
    return {
      key: g.key,
      label: g.label,
      stage: g.stage,
      group: g.group,
      reached: anyReached,
      status: terminal?.status ?? undefined,
      meta: reachedCount ? `${reachedCount} of ${children.length} reached` : undefined,
      intro:
        "One stage, Accounting & Posting, shown as four panels — the balanced " +
        "debit/credit event, a second event for any wire fee, the paired sub-ledger entries, " +
        "and the aggregated journal. The panels advance together: posting can finish before " +
        "settlement and vice-versa without the panels disagreeing on what or how much was " +
        "posted, since each carries the same accounting fact at a different grain.",
      children,
    };
  });
}

export const legTotals = (legs) => {
  const debit = sum(legs.debits);
  const credit = sum(legs.credits);
  return { debit, credit, balanced: debit === credit };
};
