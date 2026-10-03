"use client";

// Owns its own poll (one-owner rule): GET agent/reconciliation/{id}/steps every 2s while
// `active`. The agent{} record itself is polled by ScenarioStepper and passed in.

import { useEffect, useState } from "react";
import Button from "@leafygreen-ui/button";
import Badge from "@leafygreen-ui/badge";
import { H3, Body, Overline, Disclaimer } from "@leafygreen-ui/typography";
import { agentApi } from "@/lib/api/client";
import styles from "./Showcase.module.css";

const STEPS_POLL_MS = 2000;

function shortArgs(args) {
  if (!args || typeof args !== "object") return "";
  const text = Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(", ");
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

function friendly(err) {
  const s = String(err);
  if (s.startsWith("409")) return "Nothing awaiting approval.";
  if (s.startsWith("503")) return "Agent not running.";
  if (s.startsWith("502")) return `Resume failed — ${s}`;
  return s;
}

function useAgentSteps(exceptionId, active) {
  const [steps, setSteps] = useState([]);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    setSteps([]);
    setStarted(false);
  }, [exceptionId]);

  useEffect(() => {
    if (!exceptionId || !active) return undefined;
    let cancelled = false;
    let timer = null;
    const poll = async () => {
      const { data, error } = await agentApi(`reconciliation/${encodeURIComponent(exceptionId)}/steps`);
      if (cancelled) return;
      // 404 until the agent starts — keep waiting quietly.
      if (!error) {
        setSteps(data?.steps || []);
        setStarted(true);
      }
      timer = setTimeout(poll, STEPS_POLL_MS);
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [exceptionId, active]);

  return { steps, started };
}

function StepItem({ step }) {
  if (step.kind === "tool_call") {
    return (
      <li className={`${styles.tlItem} ${styles.tlTool}`}>
        → {step.tool}({shortArgs(step.args)})
      </li>
    );
  }
  if (step.kind === "tool_result") {
    const text = String(step.text || "");
    return (
      <li className={`${styles.tlItem} ${styles.tlResult}`}>
        <details>
          <summary>
            {step.tool ? `${step.tool} returned` : "result"} · {text.slice(0, 70)}
            {text.length > 70 ? "…" : ""}
          </summary>
          <pre className={styles.tlPre}>{text}</pre>
        </details>
      </li>
    );
  }
  if (step.kind === "note") {
    return <li className={`${styles.tlItem} ${styles.muted}`}>{step.text}</li>;
  }
  return <li className={`${styles.tlItem} ${styles.tlThought}`}>{step.text}</li>;
}

export default function AgentThinking({
  exceptionId,
  agent,
  lastProposal,
  policyLine,
  canDecide,
  active,
  onDecided,
}) {
  const { steps, started } = useAgentSteps(exceptionId, active);
  const [acting, setActing] = useState(null);
  const [actError, setActError] = useState(null);

  async function decide(decision) {
    if (acting) return;
    setActing(decision);
    setActError(null);
    const { error } = await agentApi(`reconciliation/${encodeURIComponent(exceptionId)}/approve`, null, {
      method: "POST",
      body: { decision, by: "presenter" },
    });
    setActing(null);
    if (error) {
      setActError(friendly(error));
      return;
    }
    onDecided?.(decision);
  }

  if (!exceptionId) {
    return (
      <div className={styles.pane}>
        <H3 className={styles.paneTitle}>Reconciliation agent</H3>
        <Body className={styles.muted}>Idle — no exception for this payment yet.</Body>
      </div>
    );
  }

  const proposal = agent?.proposedAction || lastProposal || null;
  const verified = agent?.verification?.result || null;

  return (
    <div className={styles.pane}>
      <H3 className={styles.paneTitle}>Reconciliation agent</H3>
      <Body className={styles.muted}>
        Following <span className={styles.mono}>{exceptionId}</span>
      </Body>

      <ol className={styles.timeline}>
        {!started && <li className={`${styles.tlItem} ${styles.muted}`}>Waiting for the agent to start…</li>}
        {steps.map((s, i) => (
          <StepItem key={i} step={s} />
        ))}
      </ol>

      {agent?.cause && (
        <div className={styles.section}>
          <Overline>Recorded finding</Overline>
          <div className={styles.factRow}>
            <Badge variant="red">{agent.cause}</Badge>{" "}
            {agent.confidence && <Badge variant="lightgray">confidence {agent.confidence}</Badge>}{" "}
            {agent.nextCheckAt && !verified && (
              <Badge variant="blue">recheck scheduled · {agent.recheckCount ?? 0}/3</Badge>
            )}
            {verified && <Badge variant="green">{verified}</Badge>}
          </div>
          {agent.rootCause && <Body className={styles.prose}>{agent.rootCause}</Body>}
          {(agent.evidence || []).length > 0 && (
            <ul className={styles.evidence}>
              {agent.evidence.map((ev, i) => (
                <li key={i}>{typeof ev === "string" ? ev : JSON.stringify(ev)}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {proposal && (
        <div className={styles.proposal}>
          <Overline>Proposed action</Overline>
          <Body className={styles.prose}>
            <strong>{proposal.action}</strong>
            {proposal.params?.amount != null ? ` · ${proposal.params.amount}` : ""}
            {proposal.params?.candidateIndex != null ? ` · candidate #${proposal.params.candidateIndex}` : ""}
          </Body>
          {proposal.rationale && <Body className={styles.muted}>{proposal.rationale}</Body>}
          {policyLine && <Disclaimer className={styles.policy}>Policy: {policyLine}</Disclaimer>}
          {canDecide && agent?.proposedAction && (
            <div className={styles.actions}>
              <Button variant="primary" disabled={!!acting} onClick={() => decide("APPROVE")}>
                {acting === "APPROVE" ? "Executing…" : "Approve"}
              </Button>
              <Button disabled={!!acting} onClick={() => decide("REJECT")}>
                {acting === "REJECT" ? "Rejecting…" : "Reject"}
              </Button>
            </div>
          )}
          {actError && <Body className={styles.error}>{actError}</Body>}
        </div>
      )}
    </div>
  );
}
