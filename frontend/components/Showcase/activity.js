// What the run is doing right now, so a pause never looks like a hang. The steppers decide
// the state; this file turns it into one line. Priority: a person is needed, then code is
// running, then an outside party is slow, then the agent is working.

import { phraseFor } from "./agentPhrases";

/** The tool the agent has called but not yet heard back from, in plain words. */
export function runningTool(agentSteps, bank) {
  const open = [];
  for (const s of agentSteps || []) {
    if (s.kind === "tool_call") open.push(s.tool);
    else if (s.kind === "tool_result") {
      const at = open.indexOf(s.tool);
      if (at >= 0) open.splice(at, 1);
    }
  }
  return open.length ? phraseFor(open[open.length - 1], bank) : null;
}

/**
 * @param {object} s
 * @param {string=} s.humanWait    what the presenter must do
 * @param {string=} s.running      label while a click's action runs
 * @param {string=} s.externalWait what an outside party or timer is doing
 * @param {string=} s.systemWait   a non-agent wait, e.g. a case being opened
 * @param {boolean=} s.agentBusy   the agent is working toward the next gate
 * @returns {{kind: string, text: string} | null}
 */
export function deriveActivity({ humanWait, running, externalWait, systemWait, agentBusy, agentSteps, bank }) {
  if (humanWait) return { kind: "human", text: humanWait };
  if (running) return { kind: "code", text: running };
  if (externalWait) return { kind: "external", text: externalWait };
  if (systemWait) return { kind: "code", text: systemWait };
  if (agentBusy) {
    const tool = runningTool(agentSteps, bank);
    return { kind: "agent", text: tool ? `Agent is thinking: ${tool}` : "Agent is thinking" };
  }
  return null;
}
