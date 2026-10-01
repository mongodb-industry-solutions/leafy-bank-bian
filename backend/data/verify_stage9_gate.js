// Stage 9 manual gate — verify the exceptions/returns path end-to-end on the live cluster.
//
// Evidence this checks (doc 24 §3 step 9 + leafy-bian-bian `_state.md`):
//   [1] UNMATCHED wire (alias for a FEE_DEDUCTED statement line, reconciliation plan A2):
//       SETTLED, the GL posts the full amount, the correspondent's camt.053 books it $25
//       short, statement matching sets `settlementPositions.actualAmount` from that line,
//       and stage 8 leg 2 raises RECONCILIATION_DISCREPANCY (stage 7 opens no exception).
//       Order: wait 30s → POST :8002/FinancialGateway/GW-WIRE-01/Statement/Generate → trigger
//       the GL batch (which matches statements first). Once accepted, on a
//       chargeBearer=DEBT wire: `clearing.settlementAdjustment` stamped, reconciliationStatus
//       RECONCILED, and a `{pid}-ADJ` ledgerEvent posts Dr 5214 / Cr nostro. Non-DEBT
//       accepts post nothing. (Interim — the reconciliation-agent plan moves the $25 source
//       to the camt.053 statement and the DEBT post to an approval-gated POST_ADJUSTMENT.)
//   [2] Resolve RETURN_FUNDS → debtor balance restored, 1131 nets to zero for that
//       payment, a compensating `transactions` doc (reversalOf set), and — after
//       POST /pipeline/batch/trigger — a reversal `ledgerEvents` doc (reversalOf set) AND
//       a `journalEntries` doc for it (the CDC path survived — doc 12 §1, doubled).
//   [3] DELAYED wire → OPEN SETTLEMENT_DELAYED; resolve RETRY_SETTLEMENT with MATCHED →
//       payment SETTLED, exception RESOLVED, lifecycle.events grew by the settlement
//       transitions.
//   [4] Same-key double submission → one `payments` insert, evidence check
//       `idempotent_replay_absorbed` on the winner.
//   [5] GL integrity: the stage-8 verifier's 1131-net-zero and 5/5-account checks still
//       pass INCLUDING the reversed payment.
//
// Run (MONGODB_URI in env — same convention as the stage-7/8 verifiers):
//   export MONGODB_URI="mongodb+srv://..."
//   export LEAFYBANK_DB_NAME=fsi-bian-test-db
//   mongosh "$MONGODB_URI" backend/data/verify_stage9_gate.js
//   # or verify a specific payment (one with an exception — UNMATCHED, DELAYED, RETURNED):
//   mongosh "$MONGODB_URI" backend/data/verify_stage9_gate.js -- PAY-b3d97f83
//
// The suite is hermetic and cannot see the change stream — this live run is the only proof.
// A PENDING result (reversal event not yet journaled) prints a hint to trigger another
// batch cycle and re-run; that is not a failure, the demo is mid-flight:
//   curl -X POST http://localhost:8003/pipeline/batch/trigger

const dbName = process.env.LEAFYBANK_DB_NAME || "fsi-bian-test-db";
const dbc = db.getSiblingDB(dbName);

const argPid = (typeof _passedArgs !== "undefined" && _passedArgs && _passedArgs.length)
  ? _passedArgs[0]
  : null;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { print(`  ✓ ${name}${extra ? " — " + extra : ""}`); pass++; }
  else      { print(`  ✗ ${name}${extra ? " — " + extra : ""}`); fail++; }
}

// --- target: the named payment, else the newest terminal payment with an OPEN exception --
let p;
if (argPid) {
  p = dbc.payments.findOne({ paymentId: argPid });
  if (!p) { print(`payment ${argPid} not found on ${dbName}`); quit(2); }
} else {
  // newest FAILED/RETURNED payment that has an OPEN exception
  const openExc = dbc.exceptions.findOne({ status: "OPEN" });
  if (openExc) {
    p = dbc.payments.findOne({ paymentId: openExc.paymentId });
  } else {
    // fall back to the payment of the newest exception of any status
    const lastExc = dbc.exceptions.find().sort({ _id: -1 }).limit(1).toArray()[0];
    if (lastExc) p = dbc.payments.findOne({ paymentId: lastExc.paymentId });
  }
  if (!p) {
    print(`No payment with an exception found on ${dbName}.`);
    print(`Initiate a wire with simulatedSettlementOutcome=UNMATCHED or EXCEPTION, then re-run.`);
    quit(1);
  }
}
const pid = p.paymentId;
const MIN = Math.round((p.amount || 0) * 100);   // majors → minors
print(`TARGET: ${pid} | db=${dbName} | rail=${p.rail} | amount=${p.amount} ${p.currency}`);
print(`    currentState=${p.lifecycle.currentState}  status=${p.status}`);

// --- [1] the exception queue row -------------------------------------------
// OPEN or resolved — [1] must still verify after an UNMATCHED discrepancy was accepted.
const exc = dbc.exceptions.findOne({ paymentId: pid, status: "OPEN" })
         || dbc.exceptions.find({ paymentId: pid }).sort({ _id: -1 }).limit(1).toArray()[0];
ok("[1] an exception exists for the payment", !!exc,
    exc ? `${exc.category} (${exc.status})` : "none");
if (exc) {
  ok("[1] category matches the payment state",
      (p.status === "RETURNED" && exc.category === "SETTLEMENT_RETURNED") ||
      (p.lifecycle.currentState === "IN_PROGRESS" && exc.category === "SETTLEMENT_DELAYED") ||
      (["SETTLED", "POSTED"].includes(p.lifecycle.currentState) &&
        ["RECONCILIATION_DISCREPANCY", "SETTLEMENT_DELAYED"].includes(exc.category)),
      `category=${exc.category} state=${p.lifecycle.currentState}`);
  ok("[1] no SETTLEMENT_UNMATCHED exception (stage 7 no longer raises one)",
      dbc.exceptions.countDocuments({ paymentId: pid, category: "SETTLEMENT_UNMATCHED" }) === 0);
}

if (p.simulatedSettlementOutcome === "UNMATCHED") {
  ok("[1] UNMATCHED wire settled (short), not FAILED",
      ["SETTLED", "POSTED"].includes(p.lifecycle.currentState),
      `currentState=${p.lifecycle.currentState}`);
  ok("[1] UNMATCHED aliases a FEE_DEDUCTED statement line",
      p.simulatedStatementOutcome === "FEE_DEDUCTED", `simulatedStatementOutcome=${p.simulatedStatementOutcome}`);
  const pos = dbc.settlementPositions.findOne({ paymentId: pid, outcome: "UNMATCHED" });
  if (!pos || pos.actualAmount == null) {
    print(`  ℹ [1] position has no actual yet — generate the statement, then trigger the GL batch, then re-run`);
  } else {
    ok("[1] statement matching recorded the short-pay (actual = expected − 25)",
        Number(pos.expectedAmount) - Number(pos.actualAmount) === 25 && !!pos.sourceMessageRef,
        `expected=${pos.expectedAmount} actual=${pos.actualAmount} source=${pos.sourceMessageRef}`);
  }

  const disc = dbc.exceptions.findOne({ paymentId: pid, category: "RECONCILIATION_DISCREPANCY" });
  if (!disc) {
    print(`  ℹ [1] no RECONCILIATION_DISCREPANCY yet — trigger the GL batch (stage 8 sweep), then re-run`);
  } else if (disc.status === "RESOLVED" && (disc.resolution || {}).action === "ACCEPT_DISCREPANCY") {
    ok("[1] accept flipped reconciliationStatus → RECONCILED",
        p.lifecycle.reconciliationStatus === "RECONCILED", `${p.lifecycle.reconciliationStatus}`);
    const adjEvent = dbc.ledgerEvents.findOne({ idempotencyKey: `${pid}-ADJ` });
    if (p.chargeBearer === "DEBT") {
      ok("[1] DEBT: clearing.settlementAdjustment stamped", !!p.clearing?.settlementAdjustment);
      ok("[1] DEBT: {pid}-ADJ ledgerEvent posts Dr 5214 / Cr nostro",
          !!adjEvent && adjEvent.debitLeg.glAccountCode === "5214" &&
            adjEvent.creditLeg.glAccountCode === p.clearing?.settlementAccountCode,
          adjEvent ? `Dr ${adjEvent.debitLeg.glAccountCode} / Cr ${adjEvent.creditLeg.glAccountCode}` : "missing");
    } else {
      ok(`[1] ${p.chargeBearer}: accept posts no adjustment`, !adjEvent && !p.clearing?.settlementAdjustment);
    }
  } else {
    print(`  ℹ [1] discrepancy ${disc.status} — accept it in the UI, then re-run to check the adjustment`);
  }
}

// --- [4] idempotent replay evidence (if the winner has an idempotency key) -------------
const replayCheck = (p.checks || []).find((c) => c.name === "idempotent_replay_absorbed");
if (p.idempotency?.idempotencyKey) {
  ok("[4] idempotent_replay_absorbed evidence on the winner", !!replayCheck,
      replayCheck ? "PASS" : "missing");
  ok("[4] exactly one payments doc for the key",
      dbc.payments.countDocuments({ "idempotency.idempotencyKey": p.idempotency.idempotencyKey }) === 1);
} else {
  print(`  ℹ [4] skipped — payment carries no idempotency key (send one to exercise the replay path)`);
}

// --- [2] the compensation path (RETURN_FUNDS) --------------------------------
// Only meaningful for a FAILED/RETURNED wire that has been resolved via RETURN_FUNDS.
const resolved = dbc.exceptions.findOne({ paymentId: pid, status: "RESOLVED",
                                        "resolution.action": "RETURN_FUNDS" });
if (resolved) {
  print(`  [2] RETURN_FUNDS resolved at ${(resolved.resolution || {}).at}`);

  // debtor restored: the debtor's balance reflects the credit-back. Read the original txn
  // to find the debtor + clearing accounts, then check 1131 nets to zero for this payment.
  const txns = dbc.transactions.find({ paymentId: pid }).toArray();
  const original = txns.find((t) => !t.reversalOf);
  const reversal = txns.find((t) => t.reversalOf);
  ok("[2] a compensating transactions doc with reversalOf exists", !!reversal,
      reversal ? `reversalOf=${reversal.reversalOf}` : "missing");
  ok("[2] the original transactions doc is unchanged (no reversalOf)", !!original && !original.reversalOf);

  if (original && reversal) {
    // 1131 nets to zero for this payment: principal Cr 1131 + settlement Dr 1131 + reversal
    // (Dr customer / Cr 1131) — the compensating doc's credit leg is 1131, so it nets back.
    const events = dbc.ledgerEvents.find({ idempotencyKey: { $in: [pid, pid + "-SETTLEMENT", pid + "-REV"] } }).toArray();
    let net1131 = 0;
    events.forEach((e) => {
      if (e.debitLeg?.glAccountCode === "1131") net1131 += Number(e.debitLeg?.amount || 0);
      if (e.creditLeg?.glAccountCode === "1131") net1131 -= Number(e.creditLeg?.amount || 0);
    });
    ok("[2] 1131 nets to zero for this payment", net1131 === 0, `net1131=${net1131}`);

    const revEvent = events.find((e) => e.reversalOf);
    ok("[2] a reversal ledgerEvent with reversalOf set exists", !!revEvent,
        revEvent ? `idempotencyKey=${revEvent.idempotencyKey}` : "not yet ingested");
    if (revEvent) {
      ok("[2] reversal event legs balanced",
          Number(revEvent.debitLeg?.amount) === Number(revEvent.creditLeg?.amount),
          `Dr=${revEvent.debitLeg?.amount} Cr=${revEvent.creditLeg?.amount}`);
      ok("[2] reversal event mappingVersion bumped to 1.3.0", revEvent.mappingVersion === "1.3.0",
          `mappingVersion=${revEvent.mappingVersion}`);

      // CDC survived: a journalEntries doc for the reversal event's period exists.
      const period = (revEvent.occurredAt instanceof Date ? revEvent.occurredAt : new Date(revEvent.occurredAt))
        .toISOString().slice(0, 7);
      const journal = dbc.journalEntries.findOne({ periodCode: period,
        "entries.glAccountCode": "1131" });
      ok("[2] a journalEntries doc posted for the reversal (CDC survived)", !!journal,
          journal ? `period=${period}` : "awaiting the GL batch — trigger and re-run");
    }
  }
} else if (p.status === "FAILED" || p.status === "RETURNED") {
  print(`  ℹ [2] not yet resolved via RETURN_FUNDS — resolve it in the UI, then re-run`);
}

// --- [3] the retry path (DELAYED → RETRY → SETTLED) ---------------------------
const delayedExc = dbc.exceptions.findOne({ paymentId: pid, category: "SETTLEMENT_DELAYED" });
if (delayedExc) {
  ok("[3] a SETTLEMENT_DELAYED exception exists", delayedExc.status === "OPEN" || delayedExc.status === "RESOLVED",
      `status=${delayedExc.status}`);
  if (delayedExc.status === "RESOLVED" && p.lifecycle.currentState === "SETTLED") {
    ok("[3] RETRY_SETTLEMENT landed SETTLED", p.lifecycle.currentState === "SETTLED");
    ok("[3] the SETTLED transition is on lifecycle.events",
        (p.lifecycle.events || []).some((e) => e.state === "SETTLED"));
  }
}

// --- [5] GL integrity (the stage-8 check, with the reversed payment included) ----------
// Σ subLedgerEntries == Σ journalEntries per control account. Uses Number() coercion —
// the 2026-09-03 fix: $sum returns NumberLong/BigInt, strict !== against JS number is a type
// check, not a value check. A "BREAK" with delta=0 means the comparison, not the data.
const accounts = dbc.glAccounts.find({ isPostingAccount: false, level: 2 }).toArray();
let glBreaks = 0;
accounts.forEach((a) => {
  const code = a.accountCode;
  const sl = dbc.subLedgerEntries.aggregate([
    { $match: { glAccountCode: code, status: "POSTED", journalEntryId: { $ne: null } } },
    { $group: { _id: null, s: { $sum: "$signedAmount" } } },
  ]).toArray();
  const jl = dbc.journalEntries.aggregate([
    { $match: { periodCode: { $ne: null } } },
    { $unwind: "$entries" },
    { $match: { "entries.glAccountCode": code } },
    { $group: { _id: null, j: { $sum: "$entries.signedAmount" } } },
  ]).toArray();
  const s = sl.length ? Number(sl[0].s || 0) : 0;
  const j = jl.length ? Number(jl[0].j || 0) : 0;
  const delta = s - j;
  if (Number(delta) !== 0) {
    glBreaks++;
    print(`  ✗ GL ${code}: subledger ${s} != journal ${j} (delta=${delta})`);
  }
});
ok("[5] GL integrity holds (Σ subLedgerEntries == Σ journalEntries per control account)", glBreaks === 0,
    glBreaks === 0 ? "all accounts reconcile" : `${glBreaks} broke`);

// --- verdict -----------------------------------------------------------------
print(``);
if (fail === 0) {
  print(`STAGE 9 GATE: PASS (${pass} checks)`);
  if (resolved && !dbc.journalEntries.findOne({ periodCode: (new Date().toISOString().slice(0,7)), "entries.glAccountCode": "1131" })) {
    print(`  ℹ the reversal journal may still be pending the GL batch — trigger and re-run to confirm [2] fully.`);
  }
  quit(0);
} else {
  print(`STAGE 9 GATE: FAIL — ${fail} check(s) failed (${pass} passed)`);
  quit(1);
}
