// Plain-language names for the agent's tools (backend `reconciliation_agent._build_tools`),
// so the audience reads what the agent is doing instead of `tool(args)` lines.

const PHRASES = {
  payment_trace_lookup: () => "Reading the payment's full history",
  reconciliation_analysis: () => "Comparing books with the statement",
  find_statement_candidates: () => "Searching for matching statement lines",
  get_correspondent_profile: (bank) => `Checking ${bank}'s fee schedule`,
  get_resolution_precedents: () => "Looking at how similar cases were resolved",
  record_investigation: () => "Recording the finding",
  recheck_reconciliation: () => "Re-running reconciliation",
  schedule_recheck: () => "Scheduling the next check",
  propose_action: () => "Proposing an action",
  // Cutoff agent (backend `cutoff_agent._build_tools`).
  get_payment_context: () => "Reading the held wire",
  get_cutoff_window: () => "Checking the cut-off window",
  get_stage_timing: () => "Timing the current stage against history",
  get_approval_status: () => "Checking the open approval request",
  get_screening_queue: () => "Checking the screening queue",
  get_funds_position: () => "Checking the account's funds",
  get_open_exceptions: () => "Looking for open exceptions",
  send_approval_reminder: () => "Reminding the approver",
  escalate_to_backup_approver: () => "Escalating to the backup approver",
  raise_screening_priority: () => "Moving the wire up the screening queue",
  notify_customer_funds: () => "Telling the customer about the shortfall",
  record_assessment: () => "Recording the assessment",
  propose_resolution: () => "Proposing a resolution",
};

/** Plain-language names for the cutoff agent's `actionsTaken[].action` codes. */
export const ACTION_LABEL = {
  SEND_APPROVAL_REMINDER: "Reminded the approver",
  ESCALATE_TO_BACKUP_APPROVER: "Escalated to the backup approver",
  RAISE_SCREENING_PRIORITY: "Raised the screening priority",
  NOTIFY_CUSTOMER_FUNDS: "Notified the customer of the shortfall",
  RECORD_ASSESSMENT: "Recorded its assessment",
  HOLD_NEXT_VALUE_DATE: "Held for the next value date",
  EXPEDITE: "Expedited the wire",
  DEFER_NEXT_BUSINESS_DAY: "Deferred to the next business day",
};

export const phraseFor = (tool, bank) => (PHRASES[tool] ? PHRASES[tool](bank) : String(tool));

/** One checklist item per tool call, in order; done once its tool_result arrives. */
export function evidenceItems(steps, bank) {
  const items = [];
  for (const s of steps) {
    if (s.kind === "tool_call") {
      items.push({ tool: s.tool, label: phraseFor(s.tool, bank), done: false });
    } else if (s.kind === "tool_result") {
      const open = items.find((i) => i.tool === s.tool && !i.done);
      if (open) open.done = true;
    }
  }
  return items;
}
