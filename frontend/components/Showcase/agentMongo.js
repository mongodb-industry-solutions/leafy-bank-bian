// Which MongoDB collections each agent tool reads or writes. The backend does not record
// per-call access, so this table mirrors the tool code in payment_agent/reconciliation_agent.py
// and cutoff_agent.py (and the evidence modules they call). Update it when a tool changes.
// "api" means the write goes through the transactions or ledger service, not straight to Mongo.

const TRACE = [
  ["payments", "read"], ["paymentHistory", "read"], ["paymentExecutions", "read"],
  ["transactions", "read"], ["ledgerEvents", "read"], ["settlementPositions", "read"],
  ["reconciliationItems", "read"], ["exceptions", "read"],
];
const SNAPSHOT = [["payments", "read"], ["cutoffWorkflow", "read"]];
const AGENT_RECON = ["exceptions", "write"];
const AGENT_CUTOFF = ["cutoffCases", "write"];

export const TOOL_MONGO = {
  payment_trace_lookup: TRACE,
  reconciliation_analysis: TRACE,
  find_statement_candidates: [
    ["exceptions", "read"], ["payments", "read"], ["canonicalJsonStorage", "read"],
    ["settlementPositions", "read"], AGENT_RECON,
  ],
  get_correspondent_profile: [
    ["exceptions", "read"], ["payments", "read"], ["correspondentBanks", "read"], ["settlementPositions", "read"],
  ],
  get_resolution_precedents: [["exceptions", "read"], ["payments", "read"]],
  record_investigation: [["exceptions", "read"], AGENT_RECON],
  recheck_reconciliation: [["exceptions", "read"], ["exceptions", "api"], AGENT_RECON],
  schedule_recheck: [["exceptions", "read"], AGENT_RECON],
  propose_action: [["exceptions", "read"], AGENT_RECON],

  get_payment_context: SNAPSHOT,
  get_cutoff_window: [...SNAPSHOT, ["cutoffCases", "read"]],
  get_stage_timing: SNAPSHOT,
  get_approval_status: [...SNAPSHOT, ["staffDirectory", "read"]],
  get_screening_queue: [...SNAPSHOT, ["staffDirectory", "read"]],
  get_funds_position: [...SNAPSHOT, ["accounts", "read"]],
  get_open_exceptions: [["payments", "read"], ["exceptions", "read"]],
  send_approval_reminder: [...SNAPSHOT, ["cutoffWorkflow", "write"], AGENT_CUTOFF],
  escalate_to_backup_approver: [...SNAPSHOT, ["cutoffWorkflow", "write"], AGENT_CUTOFF],
  raise_screening_priority: [...SNAPSHOT, ["cutoffWorkflow", "write"], AGENT_CUTOFF],
  notify_customer_funds: [...SNAPSHOT, ["notifications", "write"], AGENT_CUTOFF],
  record_assessment: [AGENT_CUTOFF],
  propose_resolution: [AGENT_CUTOFF],
};

/** De-duplicated `{collection, op}` list for one tool; writes sort after reads. */
export function mongoAccess(tool) {
  const seen = new Set();
  const out = [];
  for (const [collection, op] of TOOL_MONGO[tool] || []) {
    const key = `${collection}:${op}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ collection, op });
  }
  return out.sort((a, b) => (a.op === "read" ? 0 : 1) - (b.op === "read" ? 0 : 1));
}
