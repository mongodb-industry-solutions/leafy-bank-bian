"use client";

// Part D — the cut-off walkthrough's one scene. It renders progressively by act: the business
// clock against both cut-offs, the computed risk, what the agent did on its own, its
// assessment, its proposal (the decision), and the verified outcome.

import { useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import AgentTrace from "../AgentThinking";
import { ACTION_LABEL, evidenceItems, phraseFor } from "../agentPhrases";
import { cutoffError, decideCutoff } from "../cutoffActions";
import CountdownRing from "./CountdownRing";
import { asUtc } from "./format";
import styles from "../Showcase.module.css";

const ET = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const ET_SHORT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
});

export const RISK_VARIANT = { ON_TRACK: "green", AT_RISK: "yellow", WILL_MISS: "red" };
const RISK_LABEL = { ON_TRACK: "On track", AT_RISK: "At risk", WILL_MISS: "Will miss" };

const PROPOSAL_LABEL = {
  EXPEDITE: "Expedite to Fedwire",
  HOLD_NEXT_VALUE_DATE: "Hold for the next value date",
  DEFER_NEXT_BUSINESS_DAY: "Defer to the next business day",
};

export const OUTCOME_LABEL = {
  SUBMITTED_IN_TIME: "Submitted to Fedwire in time",
  HELD_NEXT_VALUE_DATE: "Held for the next value date",
  DEFERRED_NEXT_BUSINESS_DAY: "Deferred to the next business day",
  NO_ACTION_NEEDED: "No action needed",
  RELEASED: "Released by hand",
  REJECTED: "Proposal rejected",
  SUPERSEDED: "Superseded by a newer run",
  CLOCK_EXPIRED: "The cut-off passed",
};

// Actions the agent may take without asking (backend `cutoff_rules.AUTO`).
const AUTO_ACTIONS = new Set([
  "SEND_APPROVAL_REMINDER", "ESCALATE_TO_BACKUP_APPROVER", "RAISE_SCREENING_PRIORITY", "NOTIFY_CUSTOMER_FUNDS",
]);

const RESULT_VARIANT = { DONE: "green", REFUSED: "yellow", ERROR: "red" };

function countdownText(seconds) {
  if (seconds <= 0) return "passed";
  const m = Math.floor(seconds / 60);
  if (m >= 60) return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
  return `${m}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function CutoffRing({ at, businessMs, label }) {
  if (at == null) return null;
  const seconds = Math.round((at - businessMs) / 1000);
  // A one-hour ring: the last hour is the part of the day this agent cares about.
  return (
    <CountdownRing
      left={Math.max(0, seconds)}
      total={3600}
      text={countdownText(seconds)}
      label={`${label} · ${ET_SHORT.format(at)} ET`}
    />
  );
}

/** The business clock (client time plus the run's offset) against both cut-offs. */
function ClockStrip({ businessMs, risk, init }) {
  return (
    <div className={styles.clockStrip}>
      <div className={styles.section}>
        <Overline>Business time</Overline>
        <span className={styles.clockFigure}>{ET.format(businessMs)} ET</span>
      </div>
      <CutoffRing at={asUtc(risk?.internalCutoffAt)} businessMs={businessMs} label="Internal cut-off" />
      <CutoffRing at={asUtc(risk?.externalCutoffAt ?? init?.externalCutoffAt)} businessMs={businessMs} label="Fedwire cut-off" />
    </div>
  );
}

function RiskCard({ risk }) {
  if (!risk) return null;
  return (
    <div className={styles.findingCard}>
      <Overline>Risk, computed by code</Overline>
      <div className={styles.chipRow}>
        <Badge variant={RISK_VARIANT[risk.riskLevel] || "lightgray"}>{RISK_LABEL[risk.riskLevel] || risk.riskLevel}</Badge>
        {risk.phase && <Badge variant="lightgray">{risk.phase.replaceAll("_", " ").toLowerCase()}</Badge>}
        {risk.blockerType && <Badge variant="lightgray">Blocker: {risk.blockerType.toLowerCase()}</Badge>}
      </div>
      {risk.remainingP90Min != null && (
        <Body className={styles.muted}>
          Typically clears in {risk.remainingP50Min ?? "—"} min (p50), {risk.remainingP90Min} min (p90).
        </Body>
      )}
      {(risk.reasons || []).length > 0 && (
        <Body as="ul" className={styles.evidence}>
          {risk.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </Body>
      )}
    </div>
  );
}

function detailText(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  return Object.entries(detail).map(([k, v]) => `${k} ${v}`).join(", ");
}

/** What the agent did on its own: the AUTO actions and refusals, newest last. */
function ActionLog({ actions }) {
  const own = (actions || []).filter((a) => AUTO_ACTIONS.has(a.action));
  return (
    <div className={styles.findingCard}>
      <Overline>Acted on its own</Overline>
      {own.length === 0 ? (
        <Body className={styles.muted}>Nothing yet.</Body>
      ) : (
        <ul className={styles.actionLog}>
          {own.map((a, i) => (
            <li key={`${a.action}-${i}`} className={styles.checkRow}>
              <Badge variant={RESULT_VARIANT[a.result] || "lightgray"}>{a.result}</Badge>
              <Body as="span">{ACTION_LABEL[a.action] || a.action}</Body>
              {a.detail != null && <Body as="span" className={`${styles.muted} ${styles.mono}`}>{detailText(a.detail)}</Body>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The live evidence checklist, the recorded assessment, and the raw trace. */
function Assessment({ agent, steps, started, bank }) {
  const [showTrace, setShowTrace] = useState(false);
  const items = evidenceItems(steps, bank);
  const current = items.find((i) => !i.done);
  const rec = agent?.recommendation;
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
      {current && !agent?.assessedAt && <Body className={styles.muted}>{phraseFor(current.tool, bank)}…</Body>}

      {agent?.error && (
        <div className={styles.section}>
          <Overline>Assessment failed</Overline>
          <Body className={styles.error}>{agent.error.message}</Body>
          <Body className={styles.muted}>
            Attempt {agent.error.attempts}. If this is an expired AWS SSO session, run{" "}
            <span className={styles.mono}>aws sso login</span>, then use Sweep now.
          </Body>
        </div>
      )}

      {agent?.assessedAt && (
        <div className={styles.findingCard}>
          <Overline>Assessment</Overline>
          <div className={styles.chipRow}>
            {agent.blocker && <Badge variant="red">{agent.blocker}</Badge>}
            {agent.confidence && <Badge variant="lightgray">Confidence {agent.confidence}</Badge>}
            {rec?.kind && <Badge variant="blue">{rec.kind.replaceAll("_", " ").toLowerCase()}{rec.action ? `: ${rec.action}` : ""}</Badge>}
          </div>
          {agent.diagnosis && <Body>{agent.diagnosis}</Body>}
          {(agent.evidence || []).length > 0 && (
            <Body as="ul" className={styles.evidence}>
              {agent.evidence.map((ev, i) => (
                <li key={i}>{typeof ev === "string" ? ev : JSON.stringify(ev)}</li>
              ))}
            </Body>
          )}
          {agent.reasoning && <Body className={styles.muted}>{agent.reasoning}</Body>}
        </div>
      )}

      <Button size="small" onClick={() => setShowTrace((v) => !v)}>
        {showTrace ? "Hide agent trace" : "Show agent trace"}
      </Button>
      {showTrace && <AgentTrace steps={steps} started={started} />}
    </div>
  );
}

/** Act 4: the proposal, with Approve and Reject as the primary controls. */
function Proposal({ caseId, proposal, canDecide, onDecided }) {
  const [acting, setActing] = useState(null);
  const [error, setError] = useState(null);

  async function decide(decision) {
    if (acting) return;
    setActing(decision);
    setError(null);
    const { data, error: err } = await decideCutoff(caseId, decision);
    setActing(null);
    if (err) {
      setError(cutoffError(err));
      return;
    }
    onDecided(decision, data?.case || null);
  }

  if (!proposal) return <Body className={styles.muted}>No proposal to decide on.</Body>;
  return (
    <div className={styles.section}>
      <div className={styles.proposal}>
        <Overline>The agent proposes</Overline>
        <Body className={styles.proposalAction}>
          {PROPOSAL_LABEL[proposal.action] || proposal.action}
          <span className={`${styles.muted} ${styles.mono}`}> · {proposal.action}</span>
        </Body>
        {proposal.route && (
          <>
            <Overline>Approving calls</Overline>
            <Body className={styles.mono}>{proposal.route}</Body>
          </>
        )}
        {proposal.rationale && (
          <>
            <Overline>Why</Overline>
            <Body className={styles.muted}>{proposal.rationale}</Body>
          </>
        )}
        <Body className={styles.policy}>
          Policy: the agent may remind, escalate, re-prioritise and notify on its own. Expedite, hold and defer
          always wait for a human.
        </Body>
      </div>
      {canDecide && (
        <div className={styles.actions}>
          <Button variant="primary" size="large" disabled={!!acting} onClick={() => decide("APPROVE")}>
            {acting === "APPROVE" ? "Executing…" : "Approve"}
          </Button>
          <Button size="large" disabled={!!acting} onClick={() => decide("REJECT")}>
            {acting === "REJECT" ? "Rejecting…" : "Reject"}
          </Button>
        </div>
      )}
      {error && <Body className={styles.error}>{error}</Body>}
    </div>
  );
}

function Outcome({ scenario, caseDoc, init, rejected, waiting }) {
  const agent = caseDoc?.agent;
  const risk = caseDoc?.risk;
  if (rejected) {
    return (
      <div className={styles.findingCard}>
        <Overline>Run ended</Overline>
        <Body>You rejected the proposal. The wire stays on its hold, and the decision is recorded on the case.</Body>
      </div>
    );
  }
  if (!scenario.expectedOutcome) {
    // The on-track scenario: the result is the decision not to act.
    return (
      <div className={styles.section}>
        <div className={styles.chipRow}>
          <Badge variant="green">No action needed</Badge>
        </div>
        {agent?.reasoning && <Body>{agent.reasoning}</Body>}
        {init?.minutesInStage != null && risk?.remainingP90Min != null && (
          <Body className={styles.muted}>
            In this stage for {init.minutesInStage} min; it typically clears within {risk.remainingP90Min} min (p90).
          </Body>
        )}
        <RiskCard risk={risk} />
      </div>
    );
  }
  const result = caseDoc?.outcome?.result;
  if (!result) return <Body className={styles.muted}>{waiting || "Verifying the result…"}</Body>;
  const expected = result === scenario.expectedOutcome;
  return (
    <div className={styles.section}>
      <div className={styles.chipRow}>
        <Badge variant={expected ? "green" : "yellow"}>{OUTCOME_LABEL[result] || result}</Badge>
        {caseDoc.outcome.paymentStatus && <Badge variant="lightgray">{caseDoc.outcome.paymentStatus}</Badge>}
      </div>
      {caseDoc.outcome.valueDate && <Body>Value date {caseDoc.outcome.valueDate}.</Body>}
      {agent?.verification?.result && (
        <Body className={styles.muted}>The agent re-read the payment: {agent.verification.result.toLowerCase().replaceAll("_", " ")}.</Body>
      )}
      <Body className={styles.muted}>Expected: {scenario.expected}</Body>
      {!expected && (
        <Body className={styles.error}>
          This run ended differently than the scenario expects. Check the case and the payment before continuing.
        </Body>
      )}
    </div>
  );
}

// Payment statuses at or past each closing stage. A button-less scenario is already past "held".
const RELEASED = ["SUBMITTED", "SETTLED", "RECONCILED", "COMPLETED"];
const SETTLED = ["SETTLED", "RECONCILED", "COMPLETED"];

/** Held -> released -> settled -> reconciled, read from the payment itself. */
function ClosingPanel({ closing }) {
  const { copy, mode, payment, release, busy, timedOut, onFastForward } = closing;
  const status = payment?.status;
  const released = RELEASED.includes(status) || (!!release && !release.error);
  const settled = SETTLED.includes(status);
  const stages = [
    { label: "Held", done: true },
    { label: "Released", done: released },
    { label: "Settled", done: settled },
    { label: "Reconciled", done: status === "RECONCILED" },
  ];
  const valueDate = payment?.cutoff?.valueDate || payment?.valueDate;
  return (
    <div className={`${styles.findingCard} ${styles.closeOut}`}>
      <Overline>Closing the payment</Overline>
      <Body className={styles.muted}>{copy.copy}</Body>
      <ul className={styles.actionLog}>
        {stages.map((st) => (
          <li key={st.label} className={`${styles.checkRow} ${st.done ? styles.checkDone : ""}`}>
            <Icon glyph={st.done ? "Checkmark" : "Ellipsis"} size={20} />
            <Body as="span">{st.label}</Body>
            {st.label === "Settled" && st.done && valueDate && (
              <Body as="span" className={styles.muted}>value date {valueDate}</Body>
            )}
          </li>
        ))}
      </ul>
      {copy.button && !released && (
        <div className={styles.actions}>
          <Button variant="primary" disabled={busy} onClick={onFastForward}>
            {busy ? "Working…" : copy.button}
          </Button>
        </div>
      )}
      {mode === "watch" && !settled && !timedOut && <Body className={styles.muted}>Waiting for settlement…</Body>}
      {release?.error && <Body className={styles.error}>{release.error}</Body>}
      {timedOut && (
        <Body className={styles.error}>
          Still not settled after 90 seconds. Check the Payments page for its current status.
        </Body>
      )}
    </div>
  );
}

export default function CutoffScene({
  act, scenario, init, businessMs, caseDoc, steps, started, canDecide, onDecided, rejected, waiting, revisedProposal,
  closing,
}) {
  const agent = caseDoc?.agent;
  if (act === 1 && !init) return <Body>{scenario.story}</Body>;
  return (
    <div className={styles.section}>
      {init && <ClockStrip businessMs={businessMs} risk={caseDoc?.risk} init={init} />}
      {(act === 2 || act === 3) && (
        <>
          <RiskCard risk={caseDoc?.risk} />
          <ActionLog actions={agent?.actionsTaken} />
          <Assessment agent={agent} steps={steps} started={started} bank={scenario.bank} />
        </>
      )}
      {(act === 4 || revisedProposal) && (
        <>
          <Proposal caseId={caseDoc?.caseId} proposal={agent?.proposedAction} canDecide={canDecide} onDecided={onDecided} />
          <ActionLog actions={agent?.actionsTaken} />
        </>
      )}
      {act === 5 && !revisedProposal && (
        <Outcome scenario={scenario} caseDoc={caseDoc} init={init} rejected={rejected} waiting={waiting} />
      )}
      {act === 5 && !revisedProposal && closing && <ClosingPanel closing={closing} />}
    </div>
  );
}
