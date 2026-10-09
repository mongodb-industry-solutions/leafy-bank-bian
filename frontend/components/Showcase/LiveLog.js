"use client";

// Live event log. The steppers already poll the payment, the ledger trace and the agent's
// steps; this hook turns what those polls show into a feed. Each event has a stable key, so a
// re-poll of the same data never adds a second line. Newest first; new lines slide in from
// the right.

import { useEffect, useRef, useState } from "react";
import { Body, Overline } from "@leafygreen-ui/typography";
import { WriteRow } from "./StepDocuments";
import { phraseFor, ACTION_LABEL } from "./agentPhrases";
import styles from "./Showcase.module.css";

const MAX_EVENTS = 60;

export const LANES = {
  code: "Code",
  agent: "Agent",
  mongo: "MongoDB",
  human: "You",
};

const label = (value) => String(value).replaceAll("_", " ").toLowerCase();
const time = (at) => new Date(at).toLocaleTimeString([], { hour12: false });

function agentStepEvent(step, index, bank) {
  const key = `step:${index}`;
  if (step.kind === "tool_call") return { key, lane: "agent", title: phraseFor(step.tool, bank), detail: step.tool };
  if (step.kind === "tool_result") return { key, lane: "agent", title: `${step.tool ? phraseFor(step.tool, bank) : "Tool"} returned` };
  if (step.kind === "note") return null;
  return step.text ? { key, lane: "agent", title: "Reasoning", detail: String(step.text).slice(0, 140) } : null;
}

/** Events the current data implies. Keys are stable, so the hook adds only unseen ones. */
export function deriveEvents({ payment, trace, steps, agent, bank, quietWrites }) {
  const out = [];
  const life = payment?.lifecycle;
  if (life?.currentState) out.push({ key: `state:${life.currentState}`, lane: "code", title: `Payment ${label(life.currentState)}` });
  if (!quietWrites && life?.settlementStatus === "SETTLED") out.push({ key: "settle", lane: "mongo", title: "Settlement written", detail: "payments, settlementPositions" });
  if (!quietWrites && life?.postingStatus === "POSTED") out.push({ key: "posted", lane: "mongo", title: "Booked to the general ledger", detail: "journalEntries, subLedgerEntries" });
  if (life?.reconciliationStatus) out.push({ key: `recon:${life.reconciliationStatus}`, lane: "code", title: `Reconciliation ${label(life.reconciliationStatus)}` });

  (quietWrites ? [] : payment?.messages || []).forEach((m, i) => {
    const kind = m.messageType || m.type || m.format || m.purpose || "message";
    out.push({ key: `msg:${m.paymentMessageId || m._id || i}`, lane: "mongo", title: `${label(kind)} stored`, detail: "paymentMessages" });
  });
  (payment?.exceptions || []).forEach((e) => {
    out.push({ key: `exc:${e.exceptionId}`, lane: "code", title: `Exception opened: ${label(e.category || "unknown")}`, detail: "exceptions", tone: "alert" });
    if (e.status && e.status !== "OPEN") out.push({ key: `exc:${e.exceptionId}:${e.status}`, lane: "code", title: `Exception ${label(e.status)}`, tone: "good" });
  });
  (quietWrites ? [] : trace?.allLedgerEvents || []).forEach((ev) => {
    if (ev.postingStatus === "POSTED") out.push({ key: `ledger:${ev.idempotencyKey}`, lane: "mongo", title: "Ledger event posted", detail: ev.idempotencyKey });
  });

  (steps || []).forEach((s, i) => {
    const e = agentStepEvent(s, i, bank);
    if (e) out.push(e);
  });
  if (agent?.proposedAction) out.push({ key: "proposal", lane: "agent", title: "Proposed an action for approval", tone: "wait" });
  (agent?.actionsTaken || []).forEach((a, i) =>
    out.push({ key: `action:${i}:${a.action}`, lane: "agent", title: ACTION_LABEL[a.action] || label(a.action || "Action taken") })
  );
  if (agent?.verification?.result) out.push({ key: `verify:${agent.verification.result}`, lane: "code", title: `Verified: ${label(agent.verification.result)}`, tone: "good" });
  if (agent?.error) out.push({ key: "agent-error", lane: "agent", title: "Agent error", tone: "alert" });
  return out;
}

/** `resetKey` (e.g. the payment id) clears the feed when a new run starts. */
export function useLiveLog(sources, resetKey) {
  const [events, setEvents] = useState([]);
  const seen = useRef(new Set());

  const lastKey = useRef(resetKey);
  useEffect(() => {
    const previous = lastKey.current;
    lastKey.current = resetKey;
    // null -> id is the run starting, not a new run: keep what was logged so far.
    if (previous == null) return;
    seen.current = new Set();
    setEvents([]);
  }, [resetKey]);

  const { payment, trace, steps, agent, bank, quietWrites } = sources;
  useEffect(() => {
    const fresh = deriveEvents({ payment, trace, steps, agent, bank, quietWrites }).filter((e) => !seen.current.has(e.key));
    if (!fresh.length) return;
    const at = Date.now();
    fresh.forEach((e) => seen.current.add(e.key));
    // Within one batch, show the last-derived first so the feed reads newest at the top.
    setEvents((prev) => [...fresh.map((e) => ({ ...e, at })).reverse(), ...prev].slice(0, MAX_EVENTS));
  }, [payment, trace, steps, agent, bank, quietWrites]);

  /** For things only the stepper knows: a click, an approval. */
  function push(event) {
    const key = event.key || `manual:${Date.now()}:${Math.random()}`;
    if (seen.current.has(key)) return;
    seen.current.add(key);
    setEvents((prev) => [{ ...event, key, at: Date.now() }, ...prev].slice(0, MAX_EVENTS));
  }

  return { events, push };
}

export default function LiveLog({ events, sources }) {
  return (
    <section className={styles.liveLog} aria-label="Live events">
      <div className={styles.liveLogHead}>
        <Overline>Live events</Overline>
        <span className={styles.liveDot} aria-hidden="true" />
      </div>
      {events.length === 0 ? (
        <Body className={styles.muted}>Events appear here as the run progresses.</Body>
      ) : (
        <ol className={styles.liveList} aria-live="polite">
          {events.map((e) => (
            <li key={e.key} className={`${styles.liveItem} ${styles[`lane_${e.lane}`]} ${e.tone ? styles[`liveTone_${e.tone}`] : ""}`}>
              <div className={styles.liveMeta}>
                <span className={styles.liveLane}>{LANES[e.lane]}</span>
                <span className={styles.muted}>{time(e.at)}</span>
              </div>
              {e.writes ? (
                <details className={styles.liveWrites}>
                  <summary className={styles.liveTitle}>{e.title}</summary>
                  <div className={styles.writeList}>
                    {e.writes.map((w) => (
                      <WriteRow key={`${w.collection}-${w.op}`} write={w} sources={sources} />
                    ))}
                  </div>
                </details>
              ) : (
                <Body as="div" className={styles.liveTitle}>{e.title}</Body>
              )}
              {e.detail && <Body as="div" className={`${styles.muted} ${styles.mono}`}>{e.detail}</Body>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
