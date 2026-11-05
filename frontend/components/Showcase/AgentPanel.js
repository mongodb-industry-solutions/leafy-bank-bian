"use client";

// The right-hand panel of the agent walkthroughs: whether the agent is awake and why, the
// proposal waiting on the presenter, its reasoning, each tool call with the MongoDB
// collections it touched, and what it concluded. Everything comes from data the steppers
// already poll (the agent record and the checkpointed steps).

import { useEffect, useRef, useState } from "react";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import { phraseFor, ACTION_LABEL } from "./agentPhrases";
import { mongoAccess } from "./agentMongo";
import styles from "./Showcase.module.css";

const OP_LABEL = { read: "reads", write: "writes", api: "writes via API" };
const time = (at) => (at ? new Date(at).toLocaleTimeString([], { hour12: false }) : null);

/** Fold the flat step list into thoughts and tool calls (each with its result once it arrives). */
function toTimeline(steps) {
  const out = [];
  const open = [];
  steps.forEach((s, i) => {
    if (s.kind === "tool_call") {
      const call = { type: "call", key: `c${i}`, tool: s.tool, args: s.args, result: null };
      out.push(call);
      open.push(call);
    } else if (s.kind === "tool_result") {
      const at = open.findIndex((c) => c.tool === s.tool);
      if (at >= 0) open.splice(at, 1)[0].result = String(s.text || "");
    } else if (s.kind !== "note" && s.text) {
      out.push({ type: "thought", key: `t${i}`, text: plain(String(s.text)) });
    }
  });
  return out;
}

// The agent's last message restates its conclusion, which the Conclusion card below shows in
// full, so once a cause is recorded that closing thought is dropped rather than shown clamped.
function withoutClosingThought(timeline, agent) {
  const last = timeline[timeline.length - 1];
  return last?.type === "thought" && agent?.cause ? timeline.slice(0, -1) : timeline;
}

// Thoughts arrive as markdown; the panel shows plain text, so strip the markers.
const plain = (text) =>
  text
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/`([^`]+)`/g, "$1");

function touched(timeline) {
  const tally = new Map();
  for (const item of timeline) {
    if (item.type !== "call") continue;
    for (const { collection, op } of mongoAccess(item.tool)) {
      const key = `${collection}:${op}`;
      tally.set(key, { collection, op, n: (tally.get(key)?.n || 0) + 1 });
    }
  }
  return [...tally.values()].sort((a, b) => (a.op === "read" ? 0 : 1) - (b.op === "read" ? 0 : 1) || b.n - a.n);
}

const ARG_MAX = 28;
const argPairs = (args) =>
  Object.entries(args || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      const text = typeof v === "object" ? JSON.stringify(v) : String(v);
      return [k, text.length > ARG_MAX ? `${text.slice(0, ARG_MAX - 1)}…` : text];
    });
const refused = (result) => /^refused|\brefused:/i.test(result || "");

function Chip({ collection, op, n }) {
  return (
    <span className={`${styles.mongoChip} ${op === "write" || op === "api" ? styles.mongoWrite : styles.mongoRead}`} title={`${OP_LABEL[op]} ${collection}`}>
      {collection}{n > 1 ? ` ×${n}` : ""}
    </span>
  );
}

// One rail node per timeline row: a glyph in a circle, or a spinner while a call is open.
function Node({ glyph, tone, spinning }) {
  return (
    <span className={`${styles.aiNode} ${styles[`aiNode_${tone}`]}`} aria-hidden="true">
      {spinning ? <span className={styles.aiSpinner} /> : <Icon glyph={glyph} size={12} />}
    </span>
  );
}

const CLAMP_AT = 180;

function Thought({ text }) {
  const [open, setOpen] = useState(false);
  const long = text.length > CLAMP_AT;
  return (
    <li className={styles.aiItem}>
      <Node glyph="Bulb" tone="think" />
      <div className={styles.aiThoughtCard}>
        <div className={styles.aiKicker}>Thinking</div>
        <div className={`${styles.aiThoughtText} ${long && !open ? styles.aiClamp : ""}`}>{text}</div>
        {long && (
          <button type="button" className={styles.aiMore} onClick={() => setOpen(!open)}>
            {open ? "Show less" : "Show more"}
          </button>
        )}
      </div>
    </li>
  );
}

function AccessRow({ label, items }) {
  if (!items.length) return null;
  return (
    <div className={styles.aiAccess}>
      <span className={styles.aiAccessLabel}>{label}</span>
      <span className={styles.mongoChips}>{items.map((a) => <Chip key={`${a.collection}:${a.op}`} {...a} />)}</span>
    </div>
  );
}

function CallCard({ call, bank }) {
  const access = mongoAccess(call.tool);
  const pairs = argPairs(call.args);
  const running = call.result === null;
  const failed = !running && refused(call.result);
  const tone = running ? "run" : failed ? "bad" : "ok";
  return (
    <li className={styles.aiItem}>
      <Node glyph={failed ? "XWithCircle" : "Checkmark"} tone={tone} spinning={running} />
      <div className={`${styles.aiCall} ${failed ? styles.aiCallBad : ""}`}>
        <div className={styles.aiCallHead}>
          <span className={styles.aiCallTitle}>{phraseFor(call.tool, bank)}</span>
          <span className={styles.aiStatus}>{running ? "Running" : failed ? "Refused" : "Done"}</span>
        </div>
        <div className={styles.aiToolName}>
          <Icon glyph="CurlyBraces" size={11} />{call.tool}
        </div>
        {pairs.length > 0 && (
          <div className={styles.aiArgs}>
            {pairs.map(([k, v]) => (
              <span key={k} className={styles.aiArg}><span className={styles.aiArgKey}>{k}</span>{v}</span>
            ))}
          </div>
        )}
        <AccessRow label="Reads" items={access.filter((a) => a.op === "read")} />
        <AccessRow label="Writes" items={access.filter((a) => a.op !== "read")} />
        {call.result && (
          <details className={styles.aiDetails}>
            <summary><Icon glyph="CaretRight" size={10} className={styles.aiCaret} />Result</summary>
            <pre className={styles.aiPre}>{call.result}</pre>
          </details>
        )}
      </div>
    </li>
  );
}

function Findings({ agent }) {
  if (!agent) return null;
  const rows = [
    ["Cause", agent.cause || agent.diagnosis],
    ["Blocker", agent.blocker],
    ["Confidence", agent.confidence],
    ["Root cause", agent.rootCause],
    ["Reasoning", agent.reasoning],
  ].filter(([, v]) => v && typeof v === "string");
  const evidence = Array.isArray(agent.evidence) ? agent.evidence : [];
  const actions = agent.actionsTaken || [];
  if (!rows.length && !evidence.length && !actions.length) return null;
  return (
    <section className={styles.aiConclusion}>
      <div className={styles.aiConclusionHead}><Icon glyph="Wizard" size={14} />Conclusion</div>
      {rows.length > 0 && (
        <dl className={styles.aiFacts}>
          {rows.map(([label, value]) => (
            <div key={label} className={styles.aiFact}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      )}
      {evidence.length > 0 && (
        <>
          <div className={styles.aiKicker}>Evidence</div>
          <ul className={styles.featureList}>
            {evidence.map((e) => <li key={String(e)}>{typeof e === "string" ? e : JSON.stringify(e)}</li>)}
          </ul>
        </>
      )}
      {actions.length > 0 && (
        <>
          <div className={styles.aiKicker}>Actions taken</div>
          <ul className={styles.aiActions}>
            {actions.map((a, i) => (
              <li key={`${a.action}:${i}`} className={a.result === "REFUSED" ? styles.checkBad : styles.checkOk}>
                <Icon glyph={a.result === "REFUSED" ? "XWithCircle" : "CheckmarkWithCircle"} size={14} />
                <span>{ACTION_LABEL[a.action] || a.action} <span className={styles.muted}>({a.result})</span></span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/**
 * @param {object} p
 * @param {object|null} p.agent       the agent sub-document (exceptions.agent / cutoffCases.agent)
 * @param {object[]} p.steps          GET .../steps
 * @param {boolean} p.started         the steps endpoint has answered
 * @param {{kind: string, text: string}|null} p.activity  the run's current wait, for the status line
 * @param {{text: string, at?: any}|null} p.activation  why the agent woke
 * @param {string=} p.bank            correspondent bank, for tool phrases
 * @param {boolean=} p.needsDecision  a proposal waits on the presenter (the decision itself is docked by RunConsole)
 */
export default function AgentPanel({ agent, steps, started, activity, activation, bank, needsDecision, children }) {
  const timeline = withoutClosingThought(toTimeline(steps || []), agent);
  const collections = touched(timeline);
  const toolCalls = timeline.filter((t) => t.type === "call").length;
  const bodyRef = useRef(null);
  const [follow, setFollow] = useState(true);

  useEffect(() => {
    const el = bodyRef.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [timeline.length, follow]);

  const working = activity?.kind === "agent";
  const waitsOnYou = activity?.kind === "human" && (!!children || !!needsDecision);
  const state = agent?.error && !agent?.verification ? "Failed"
    : waitsOnYou ? "Waiting for you"
      : working ? "Thinking"
        : agent?.verification || agent?.outcome ? "Done"
          : started || agent ? "Idle" : "Not started";
  const stateClass = { Thinking: styles.aiThinking, "Waiting for you": styles.aiWaiting, Failed: styles.aiFailed }[state] || "";
  const activatedAt = time(activation?.at || agent?.startedAt);

  return (
    <aside className={styles.aiPanel} aria-label="AI agent">
      <header className={styles.aiHead}>
        <span className={styles.aiTitle}><Icon glyph="Sparkle" size={16} />AI agent</span>
        <span className={`${styles.aiState} ${stateClass}`}>
          {working && <span className={styles.liveDot} aria-hidden="true" />}
          {state}
        </span>
      </header>
      {activation && (
        <Body as="div" className={styles.muted}>
          {activation.text}{activatedAt ? ` · ${activatedAt}` : ""}
        </Body>
      )}

      {children}

      <div className={styles.aiBody} ref={bodyRef} onScroll={(e) => {
        const el = e.currentTarget;
        setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
      }}>
        <section className={styles.aiSection}>
          <div className={styles.aiSectionHead}>
            <Overline>Thinking and tool calls</Overline>
            {timeline.length > 0 && (
              <span className={styles.muted}>
                {toolCalls} tool call{toolCalls === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {collections.length > 0 && (
            <div className={styles.aiTouched} aria-label="MongoDB collections touched so far">
              <Icon glyph="Database" size={12} />
              <span className={styles.mongoChips}>
                {collections.map((c) => <Chip key={`${c.collection}:${c.op}`} {...c} />)}
              </span>
            </div>
          )}
          {timeline.length === 0 ? (
            <Body className={styles.muted}>
              {started ? "The agent has not called a tool yet." : "Nothing yet. The agent wakes when the case opens."}
            </Body>
          ) : (
            <ol className={styles.aiTimeline}>
              {timeline.map((item) => (item.type === "thought"
                ? <Thought key={item.key} text={item.text} />
                : <CallCard key={item.key} call={item} bank={bank} />))}
            </ol>
          )}
        </section>
        <Findings agent={agent} />
      </div>
    </aside>
  );
}
