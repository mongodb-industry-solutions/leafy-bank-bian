"use client";

// The raw agent trace behind the "Show agent trace" toggle, plus the steps poll hook
// (GET agent/{path}/{id}/steps every 2s while `active`). The steppers own the hook.

import { useEffect, useState } from "react";
import { Body } from "@leafygreen-ui/typography";
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

// `path` is the agent's case collection: "reconciliation" (exceptions) or "cutoff/cases".
export function useAgentSteps(exceptionId, active, path = "reconciliation") {
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
      const { data, error } = await agentApi(`${path}/${encodeURIComponent(exceptionId)}/steps`);
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
  }, [exceptionId, active, path]);

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

/** The unprocessed timeline: thoughts, tool calls with arguments, tool results. */
export default function AgentTrace({ steps, started }) {
  return (
    <ol className={styles.timeline}>
      {!started && <Item className={styles.muted}>Waiting for the agent to start…</Item>}
      {steps.map((s, i) => (
        <StepItem key={i} step={s} />
      ))}
    </ol>
  );
}
