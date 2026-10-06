"use client";

import { useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import AgentTrace from "../AgentThinking";
import { evidenceItems, phraseFor } from "../agentPhrases";
import { friendlyError, retryInvestigation } from "../agentActions";
import { major } from "./format";
import styles from "../Showcase.module.css";

/**
 * Policy lines come only from what the backend recorded: the agent's cause, the scenario's
 * rule, and the proposed action. A proposal that exists has already passed `check_proposal`.
 * `ok: false` marks a refusal the scenario is designed around (R4's FEE).
 */
function policyLines(agent, scenario, gapMajor) {
  if (!agent?.cause) return [];
  const { cause, constraint, action } = scenario.decision;
  const refused = String(constraint.code).endsWith("refused");
  const lines = [
    { text: `Cause ${agent.cause}: ${cause.label}`, ok: true },
    { text: `Rule: ${constraint.label}`, ok: !refused },
  ];
  const proposal = agent.proposedAction;
  if (proposal) {
    lines.push({ text: `${proposal.action} is the allowed action: ${action.label}`, ok: true });
    const amount = proposal.params?.amount;
    if (amount != null && gapMajor != null) {
      lines.push({
        text: `Adjustment ${major(amount)} ${Number(amount) === gapMajor ? "equals" : "differs from"} the gap ${major(gapMajor)}`,
        ok: Number(amount) === gapMajor,
      });
    }
    const target = proposal.params?.target;
    if (target) {
      lines.push({ text: `Linked only to a returned candidate: ${target.reference}${target.score != null ? ` (score ${target.score})` : ""}`, ok: true });
    }
  } else if (agent.nextCheckAt || agent.recheckCount) {
    lines.push({ text: `${action.code} moves no money, so it ran without asking anyone`, ok: true });
  }
  return lines;
}

/** Act 3: live evidence checklist, then the finding and the policy check. */
export default function InvestigateScene({ scenario, exceptionId, paymentId, agent, steps, started, gapMajor }) {
  const [showTrace, setShowTrace] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState(null);
  const items = evidenceItems(steps, scenario.bank);
  const verified = agent?.verification?.result;
  const working = !agent?.proposedAction && !agent?.nextCheckAt && !verified && !agent?.error;
  const current = items.find((i) => !i.done);
  const lines = policyLines(agent, scenario, gapMajor);

  async function retry() {
    setRetrying(true);
    setRetryError(null);
    const { error } = await retryInvestigation(exceptionId, paymentId);
    setRetrying(false);
    if (error) setRetryError(friendlyError(error));
  }

  if (!exceptionId) return <Body className={styles.muted}>Waiting for the exception to open…</Body>;

  return (
    <div className={styles.section}>
      <ul className={styles.checklist}>
        {!started && <Body className={styles.muted}>Waiting for the agent to start…</Body>}
        {items.map((it, i) => (
          <li key={`${it.tool}-${i}`} className={`${styles.checkRow} ${it.done ? styles.checkDone : ""}`}>
            <Icon glyph={it.done ? "Checkmark" : "Ellipsis"} size={20} />
            <Body as="span">{it.label}</Body>
          </li>
        ))}
      </ul>
      {working && current && <Body className={styles.muted}>{phraseFor(current.tool, scenario.bank)}…</Body>}

      {agent?.error && !verified && (
        <div className={styles.section}>
          <Overline>Investigation failed</Overline>
          <Body className={styles.error}>{agent.error.message}</Body>
          <Body className={styles.muted}>
            Retrying automatically (attempt {agent.error.attempts}). If this is an expired AWS SSO
            session, run <span className={styles.mono}>aws sso login</span> first.
          </Body>
          <Button size="small" disabled={retrying} onClick={retry}>
            {retrying ? "Retrying…" : "Retry investigation"}
          </Button>
          {retryError && <Body className={styles.error}>{retryError}</Body>}
        </div>
      )}

      {agent?.cause && (
        <div className={styles.findingCard}>
          <Overline>Finding</Overline>
          <div className={styles.chipRow}>
            <Badge variant="red">{agent.cause}</Badge>
            {agent.confidence && <Badge variant="lightgray">Confidence {agent.confidence}</Badge>}
            {(agent.nextCheckAt || agent.recheckCount > 0) && !agent.proposedAction && (
              <Badge variant="green">Acted without approval: RECHECK</Badge>
            )}
            {agent.nextCheckAt && !verified && (
              <Badge variant="blue">Recheck {agent.recheckCount ?? 0} of 3 scheduled</Badge>
            )}
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

      {lines.length > 0 && (
        <div className={styles.findingCard}>
          <Overline>Policy check</Overline>
          <ul className={styles.checklist}>
            {lines.map((l) => (
              <li key={l.text} className={`${styles.checkRow} ${styles.checkDone}`}>
                <Icon glyph={l.ok ? "Checkmark" : "X"} size={20} className={l.ok ? undefined : styles.refusedIcon} />
                <Body as="span">{l.text}</Body>
              </li>
            ))}
          </ul>
        </div>
      )}

      <Button size="small" onClick={() => setShowTrace((v) => !v)}>
        {showTrace ? "Hide agent trace" : "Show agent trace"}
      </Button>
      {showTrace && <AgentTrace steps={steps} started={started} />}
    </div>
  );
}
