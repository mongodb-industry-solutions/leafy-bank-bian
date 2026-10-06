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
