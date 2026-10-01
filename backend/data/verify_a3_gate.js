// Reconciliation plan A3 live gate — the MISSING watchdog, orphan lines, upserted items.
//
// Run, with STATEMENT_EXPECTED_WITHIN_SECONDS=60 on the transactions service:
//   1. Initiate a CLEAN outbound wire (Demo Controls default). Do NOT generate a statement.
//   2. Wait ~30s (settle) + 60s (window), then trigger the GL batch:
//        curl -X POST http://localhost:8003/pipeline/batch/trigger
//   3. Run this with the payment id → [1] expects an OPEN RECONCILIATION_MISSING.
//   4. Generate the statement (POST :8002/FinancialGateway/GW-WIRE-01/Statement/Generate
//      {"accountCode":"1111"}), then  curl -X POST "localhost:8003/pipeline/reconcile/<pid>?match=true"
//   5. Re-run this → [1] expects the MISSING RESOLVED by RECHECK and the payment RECONCILED.
//
//   mongosh "$MONGODB_URI" --eval 'globalThis._passedArgs = ["PAY-…"];' backend/data/verify_a3_gate.js
//
// ⚠️ Read the TARGET line first — an arg that did not arrive retargets nothing here, it
//    fails loudly, but check the count on the verdict line against the checks below
//    (defect 2026-10-01: a skipped block prints nothing).

const dbName = process.env.LEAFYBANK_DB_NAME || "fsi-bian-test-db";
const dbc = db.getSiblingDB(dbName);
const pid = (typeof _passedArgs !== "undefined" && _passedArgs && _passedArgs.length) ? _passedArgs[0] : null;
if (!pid) { print("usage: pass the payment id via --eval 'globalThis._passedArgs = [\"PAY-…\"];'"); quit(2); }

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { print(`  ✓ ${name}${extra ? " — " + extra : ""}`); pass++; }
  else      { print(`  ✗ ${name}${extra ? " — " + extra : ""}`); fail++; }
}

const p = dbc.payments.findOne({ paymentId: pid });
if (!p) { print(`payment ${pid} not found on ${dbName}`); quit(2); }
print(`TARGET ${pid} · state ${p.lifecycle?.currentState} · recon ${p.lifecycle?.reconciliationStatus}`);

// --- [1] the watchdog ---------------------------------------------------------
const pos = dbc.settlementPositions.find({ paymentId: pid }).sort({ createdAt: -1 }).limit(1).toArray()[0];
ok("[1] position carries expectedWindow.by", !!pos?.expectedWindow?.by, String(pos?.expectedWindow?.by));
const missing = dbc.exceptions.find({ paymentId: pid, category: "RECONCILIATION_MISSING" }).toArray();
ok("[1] at most one RECONCILIATION_MISSING occurrence", missing.length <= 1, `${missing.length} found`);
if (pos?.actualAmount == null) {
  ok("[1] statement line not matched → MISSING is OPEN",
     missing.length === 1 && missing[0].status === "OPEN", missing[0]?.status);
  ok("[1] payment not marked DISCREPANT while missing", p.lifecycle?.reconciliationStatus !== "DISCREPANT");
} else {
  ok("[1] statement line matched → MISSING closed by RECHECK",
     missing.length === 0 || (missing[0].status === "RESOLVED" && missing[0].resolution?.action === "RECHECK"),
     missing[0] ? `${missing[0].status}/${missing[0].resolution?.action}` : "never raised");
}

// --- [2] one open reconciliation item per payment -----------------------------
const items = dbc.reconciliationItems.find({ paymentId: pid }).toArray();
const openItems = items.filter((i) => i.overallResult !== "RECONCILED");
ok("[2] at most one open reconciliationItems doc", openItems.length <= 1, `${items.length} total, ${openItems.length} open`);
ok("[2] payment refs the current item",
   !items.length || items.some((i) => i.reconciliationItemId === p.refs?.reconciliationItemId),
   p.refs?.reconciliationItemId);

// --- [3] orphans: every unmatched statement line is queued exactly once -------
let lines = 0, queued = 0, dupes = 0;
dbc.paymentMessages.find({ purpose: "ACCOUNT_STATEMENT", "entries.recon.status": "UNMATCHED" }).forEach((s) => {
  s.entries.filter((e) => e.recon?.status === "UNMATCHED").forEach((e) => {
    lines++;
    const n = dbc.exceptions.countDocuments({ paymentId: `${s.paymentMessageId}#${e.lineNo}`, category: "ORPHANED_SETTLEMENT" });
    if (n >= 1 && e.recon.exceptionId) queued++;
    if (n > 1) dupes++;
  });
});
ok("[3] every unmatched statement line has its ORPHANED_SETTLEMENT", queued === lines, `${queued}/${lines}`);
ok("[3] no line queued twice", dupes === 0, `${dupes} duplicated`);

print("");
// 8 checks expected (both [1] branches print 2 after the 2 shared ones).
if (fail === 0) { print(`A3 GATE: PASS (${pass}/8 checks)`); quit(0); }
print(`A3 GATE: FAIL — ${fail} failed (${pass} passed, 8 expected)`); quit(1);
