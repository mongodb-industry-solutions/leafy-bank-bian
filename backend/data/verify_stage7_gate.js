// Stage 7 manual gate — verify an external wire settled end-to-end on the live cluster.
//
// Evidence this checks (leaf-bank-bian `_state.md` line 25 + doc 21 §4):
//   SETTLED lifecycle event · settlementStatus=SETTLED · a `settlementPositions` doc ·
//   a `transactions` doc (the halt is gone) · the PRINCIPAL and SETTLEMENT ledger events,
//   each reaching a journalEntry after the GL batch · the 1131 clearing position netting
//   to zero across the principal (Cr) + settlement (Dr) legs.
//
// ledgerEvents is keyed by `idempotencyKey` (= paymentId / {paymentId}-FEE /
// {paymentId}-SETTLEMENT), NOT by `paymentId` — query it by that, the same way
// pipeline_read_service does (doc 21 step 8).
//
// Run (MONGODB_URI in env — same convention as the sibling load scripts):
//   export MONGODB_URI="mongodb+srv://..."
//   export LEAFYBANK_DB_NAME=fsi-bian-test-db            # your dev DB; Kanopy: leafy-bank-bian
//   mongosh "$MONGODB_URI" backend/data/verify_stage7_gate.js
//
// If postingStatus is still PENDING / journalEntryId null, the GL batch has not run yet —
// trigger it, wait a minute, and re-run:
//   curl -X POST http://localhost:8003/pipeline/batch/trigger
//
// If the SETTLEMENT event is missing entirely, the settlement_worker was not running (or
// had no resume token) when the payment settled — change streams never replay a past
// update. Restart the ledger service so the worker connects, then create + settle a NEW
// external wire; on a settled cluster a fresh SURVEY runs clean.

const dbName = process.env.LEAFYBANK_DB_NAME || "fsi-bian-test-db";
const dbc = db.getSiblingDB(dbName);

// --- target: newest externally-settled payment ---------------------------------
const p = dbc.payments.find({ "lifecycle.settlementStatus": "SETTLED" })
                       .sort({ initiatedAt: -1 }).limit(1).next();
if (!p) {
  print(`NO settled payment found on ${dbName} — create an external wire, settle it`);
  print(`(POST /PaymentSettlement/Initiate), run the GL batch, then re-run.`);
  print("Recent payments:");
  dbc.payments.find({}, { paymentId: 1, rail: 1, status: 1, "lifecycle.currentState": 1,
                          "lifecycle.settlementStatus": 1, initiatedAt: 1 })
       .sort({ initiatedAt: -1 }).limit(5).forEach(d => print("  ", EJSON.stringify(d)));
  quit(1);
}
const pid = p.paymentId;
print(`TARGET: ${pid} | db=${dbName} | rail=${p.rail} | amount=${p.amount} ${p.currency} | isInternal=${p.isInternal}`);
print("    initiatedAt:", EJSON.stringify(p.initiatedAt));

// 1. lifecycle: SETTLED reached + stage-7 events; postingStatus from stage 6
print("\n[1] lifecycle.currentState =", p.lifecycle.currentState, "| settlementStatus =",
      p.lifecycle.settlementStatus, "| postingStatus =", (p.lifecycle.postingStatus || "null"));
print("    last events:");
p.lifecycle.events.slice(-6).forEach(e =>
  print("      " + e.state.padEnd(14), String(e.actor).slice(0, 26).padEnd(26), e.reason));

// 2. stage-7 checks
print("\n[2] stage-7 checks:");
p.checks.filter(c => String(c.stage).startsWith("7 ")).forEach(c =>
  print("      " + c.result.padEnd(4), c.name.padEnd(28), c.detail));

// 3. clearing block + refs
console.log("\n[3] clearing.settledAt =", p.clearing.settledAt, "| batchRef =", p.clearing.batchRef,
            "| settlementAccountCode =", p.clearing.settlementAccountCode);
print("    refs.ledgerEventId =", p.refs.ledgerEventId, "| refs.journalEntryId =", p.refs.journalEntryId,
      "| refs.transactionId =", p.refs.transactionId);

// 4. settlementPositions (one doc, traceable both ways)
const pos = dbc.settlementPositions.findOne({ paymentId: pid });
print("\n[4] settlementPositions:", pos ? JSON.stringify({
      settlementPositionId: pos.settlementPositionId, model: pos.model, modelLabel: pos.modelLabel,
      clearingAccountCode: pos.clearingAccountCode, settlementAccountCode: pos.settlementAccountCode,
      grossAmount: pos.grossAmount, outcome: pos.outcome, status: pos.settlementStatus, batchRef: pos.batchRef,
    }) : "MISSING");

// 5. boundary: this wire wrote a transactions doc
const tx = dbc.transactions.findOne({ paymentId: pid }, { _id: 0, payer: 1, payee: 1, amount: 1,
      txnId: 1, type: 1, settledAt: 1, currency: 1 });
print("\n[5] transactions doc:", tx ? JSON.stringify({
      txnId: tx.txnId, type: tx.type, amount: tx.amount, payer: (tx.payer || {}).accountId,
      payee: (tx.payee || {}).accountId, settledAt: tx.settledAt && EJSON.stringify(tx.settledAt),
    }) : "MISSING — halt not removed");

// 6. ledgerEvents keyed by idempotencyKey: principal + fee + settlement
print("\n[6] ledgerEvents (by idempotencyKey):");
const KEYS = [pid, pid + "-FEE", pid + "-SETTLEMENT"];
const evs = dbc.ledgerEvents.find({ idempotencyKey: { $in: KEYS } }).toArray();
const byKey = Object.fromEntries(evs.map(e => [e.idempotencyKey, e]));
let net1131 = 0;
evs.forEach(e => {
  const dr = e.debitLeg, cr = e.creditLeg;
  if (dr.glAccountCode === "1131") net1131 -= dr.amount;
  if (cr.glAccountCode === "1131") net1131 += cr.amount;
  print("    " + (e.idempotencyKey || "").padEnd(30), (e.eventType || "").padEnd(22),
        "posting=" + (e.postingStatus || "").padEnd(7),
        "DR " + dr.glAccountCode + "(" + dr.amount + ")", "CR " + cr.glAccountCode + "(" + cr.amount + ")");
});
KEYS.forEach(k => { if (!byKey[k]) print("    " + k.padEnd(30), "ABSENT"); });
const hasPrincipal = !!byKey[pid];
const hasSettlement = !!byKey[pid + "-SETTLEMENT"];
print("    1131 netting (principal CR + settlement DR) =", net1131, "  <- must be 0");

// 7. subledger rows — journalEntryId lives HERE (on the subledger rows), not on the
//    ledgerEvents doc. rows=2 per event (DR + CR); the stamped journalEntryId is what
//    ties each event into the journal.
print("\n[7] subLedger entries per event (sourceReference.sourceId = eventId):");
const journaledEventIds = [];
evs.forEach(e => {
  const rows = dbc.subLedgerEntries.find({ "sourceReference.sourceId": e.eventId }).toArray();
  const jids = [...new Set(rows.map(r => r.journalEntryId))];
  if (rows.length >= 2 && jids.length === 1 && jids[0]) journaledEventIds.push(e.eventId);
  print("    " + (e.idempotencyKey || "").padEnd(30), "rows=" + rows.length,
        "journalEntryIds=" + JSON.stringify(jids));
});
const allJournaled = evs.length > 0 && journaledEventIds.length === evs.length;

// 7b. the journal doc itself exists and is POSTED
const jnlId = byKey[pid] && dbc.subLedgerEntries.findOne(
    { "sourceReference.sourceId": byKey[pid].eventId })?.journalEntryId;
const jnl = jnlId ? dbc.journalEntries.findOne({ journalId: jnlId }) : null;
print("\n[7b] journal:", jnl ? JSON.stringify({
      journalId: jnl.journalId, status: jnl.status, periodCode: jnl.periodCode,
      entries: (jnl.entries || []).map(x => x.side + " " + x.accountCode + "(" + x.amount + ")").join(", "),
    }) : "MISSING — GL batch has not run");

// 8. clearing account — informational only. The position relief lives in the GL legs
//    (principal Cr 1131 ⇄ settlement Dr 1131), NOT in accounts.balance: the ledger
//    service never writes `accounts` (firewall), so this balance is the in-flight
//    holding mirror and a nonzero value is expected.
const ca = dbc.accounts.findOne({ accountId: "ACC-CLEARING-WIRE" },
    { accountId: 1, type: 1, status: 1, gl: 1, "balance.current": 1, "balance.ledger": 1 });
print("\n[8] ACC-CLEARING-WIRE (info):", ca ? JSON.stringify({
      type: ca.type, status: ca.status, gl: ca.gl,
      balance: { current: ca.balance.current, ledger: ca.balance.ledger },
    }) : "MISSING — seed failed (run load_sample_seed.py)");

const verdict = (p.lifecycle.currentState === "SETTLED" && p.lifecycle.settlementStatus === "SETTLED"
  && pos && hasPrincipal && hasSettlement && net1131 === 0 && allJournaled && jnl && tx)
  ? "PASS — stage 7 gate evidence is complete"
  : "CHECK ABOVE — one or more expectations above are unmet";
print("\nVERDICT:", verdict);
