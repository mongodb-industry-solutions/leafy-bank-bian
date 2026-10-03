"use client";

// Owns its own poll (one-owner rule): GET agent/reconciliation/{id}/steps every 2s while
// `active`. The agent{} record itself is polled by ScenarioStepper and passed in.

import { useEffect, useState } from "react";
import Button from "@leafygreen-ui/button";
import Badge from "@leafygreen-ui/badge";
import { H3, Body, Overline } from "@leafygreen-ui/typography";
import { agentApi } from "@/lib/api/client";
import styles from "./Showcase.module.css";

const STEPS_POLL_MS = 2000;

const Item = ({ className, children }) => (
  <li>
    <Body as="div" className={className}>{children}</Body>
  </li>
);

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
      <Item className={styles.tlTool}>
        → {step.tool}({shortArgs(step.args)})
      </Item>
    );
  }
  if (step.kind === "tool_result") {
    const text = String(step.text || "");
    return (
      <Item className={styles.muted}>
        <details>
          <summary>
            {step.tool ? `${step.tool} returned` : "result"} · {text.slice(0, 70)}
            {text.length > 70 ? "…" : ""}
          </summary>
          <pre className={styles.tlPre}>{text}</pre>
        </details>
      </Item>
    );
  }
  if (step.kind === "note") {
    return <Item className={styles.muted}>{step.text}</Item>;
  }
  return <Item className={styles.tlThought}>{step.text}</Item>;
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
        <Body className={styles.muted}>Idle until reconciliation opens an exception.</Body>
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
        {!started && <Item className={styles.muted}>Waiting for the agent to start…</Item>}
        {steps.map((s, i) => (
          <StepItem key={i} step={s} />
        ))}
      </ol>

      {agent?.cause && (
        <div className={styles.section}>
          <Overline>Finding</Overline>
          <div className={styles.factRow}>
            <Badge variant="red">{agent.cause}</Badge>{" "}
            {agent.confidence && <Badge variant="lightgray">Confidence {agent.confidence}</Badge>}{" "}
            {agent.nextCheckAt && !verified && (
              <Badge variant="blue">Recheck {agent.recheckCount ?? 0} of 3 scheduled</Badge>
            )}
            {verified && <Badge variant="green">{verified}</Badge>}
          </div>
          {agent.rootCause && <Body>{agent.rootCause}</Body>}
          {(agent.evidence || []).length > 0 && (
            <Body as="ul" className={styles.evidence}>
              {agent.evidence.map((ev, i) => (
                <li key={i}>{typeof ev === "string" ? ev : JSON.stringify(ev)}</li>
              ))}
            </Body>
          )}
        </div>
      )}

      {proposal && (
        <div className={styles.proposal}>
          <Overline>Proposed action</Overline>
          <Body>
            <strong>{proposal.action}</strong>
            {proposal.params?.amount != null ? ` · ${proposal.params.amount}` : ""}
            {proposal.params?.candidateIndex != null ? ` · candidate #${proposal.params.candidateIndex}` : ""}
          </Body>
          {proposal.rationale && <Body className={styles.muted}>{proposal.rationale}</Body>}
          {policyLine && <Body className={styles.policy}>Policy: {policyLine}</Body>}
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
