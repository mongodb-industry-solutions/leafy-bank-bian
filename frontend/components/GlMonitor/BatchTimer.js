"use client";

// Compact countdown to the next GL batch + a manual "Run batch now" trigger,
// sized to sit inline in the Initiate panel header next to Refresh All.
//
// The ring counts down to `nextBatchAt` — the authoritative next-run timestamp
// the gl_batch worker publishes each cycle (it owns the real sleep clock, so
// this is exact, unlike lastBatchAt + interval which goes stale on idle cycles).
// When the target elapses, we re-poll /health to pick up the worker's freshly
// scheduled target (idle cycles never advance lastBatchAt to trigger a parent
// refresh).
//
// The button POSTs /pipeline/batch/trigger. That runs one cycle on demand but
// does NOT reset the periodic worker's schedule, so the ring keeps counting to
// the next scheduled run; we just call onTriggered to refresh columns/dashboard.
import { useEffect, useRef, useState } from "react";
import { pipelineApi } from "@/lib/api/client";
import styles from "./GlMonitor.module.css";

const R = 9;
const CIRC = 2 * Math.PI * R;

function fmt(secs) {
  const s = Math.max(0, Math.round(secs));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

const toMs = (iso) => (iso ? new Date(iso).getTime() : null);

export default function BatchTimer({ nextBatchAt, intervalSeconds = 600, onTriggered }) {
  const [now, setNow] = useState(() => Date.now());
  const [nextAt, setNextAt] = useState(() => toMs(nextBatchAt));
  const [firing, setFiring] = useState(false);

  // Adopt a fresher target when the parent supplies one.
  useEffect(() => {
    const t = toMs(nextBatchAt);
    if (t != null) setNextAt(t);
  }, [nextBatchAt]);

  // 1s clock.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Once the target passes, poll /health (throttled ~2s) until the worker
  // advances nextRunAt, then the ring resets to the new schedule.
  const settling = useRef(false);
  useEffect(() => {
    if (nextAt == null || now < nextAt || settling.current) return;
    settling.current = true;
    (async () => {
      const { data } = await pipelineApi("health");
      const t = toMs(data?.nextBatchAt);
      if (t != null) setNextAt(t);
      setTimeout(() => {
        settling.current = false;
      }, 2000);
    })();
  }, [now, nextAt]);

  async function handleTrigger() {
    setFiring(true);
    const { error: err } = await pipelineApi("batch/trigger", null, { method: "POST" });
    setFiring(false);
    if (!err) onTriggered?.();
  }

  const remaining = nextAt != null ? (nextAt - now) / 1000 : intervalSeconds;
  const overdue = nextAt != null && remaining <= 0;
  const frac = Math.min(1, Math.max(0, remaining / intervalSeconds));
  const dash = CIRC * frac;

  return (
    <div className={styles.batchTimer}>
      <svg
        width="22"
        height="22"
        viewBox="0 0 22 22"
        title={nextAt == null ? "Waiting for schedule" : "Next GL batch"}
      >
        <circle cx="11" cy="11" r={R} fill="none" stroke="var(--border)" strokeWidth="3" />
        <circle
          cx="11"
          cy="11"
          r={R}
          fill="none"
          stroke={overdue ? "var(--green)" : "var(--accent)"}
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={CIRC}
          strokeDashoffset={CIRC - dash}
          transform="rotate(-90 11 11)"
          style={{ transition: "stroke-dashoffset 1s linear" }}
        />
      </svg>
      <span className={styles.batchTimerTime}>{overdue ? "now" : fmt(remaining)}</span>
      <span className={styles.batchTimerLabel}>next batch</span>
      <button
        className={styles.btn}
        onClick={handleTrigger}
        disabled={firing}
        title="Run the GL batch now"
      >
        {firing ? "Running…" : "▶ Run batch"}
      </button>
    </div>
  );
}
