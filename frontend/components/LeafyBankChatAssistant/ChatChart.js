"use client";

/**
 * Renders a `chart` spec emitted by the customer360 agent
 * ({ type: "bar" | "donut", title, currency, data: [{ label, value }] } — see
 * magenta-customer360-agents financial_wellness insights_service.py).
 *
 * Plain HTML/SVG, no chart dependency. Colors are Via design-system
 * primitives; the donut order was run through the dataviz palette validator
 * (CVD/normal-vision separation + 3:1 contrast on white) — keep the order if
 * you change it. Identity never relies on color alone: every mark has its
 * label and value in text, and a table view is one click away.
 */

import { useState } from "react";
import styles from "./ChatChart.module.css";

// Via green-600 for single-series bars; categorical order for the donut.
const BAR_COLOR = "#00684A";
const DONUT_COLORS = ["#00A35C", "#B45AF2", "#944F01", "#016BF8", "#DB3030"];
const OTHER_COLOR = "#889397"; // Via neutral-400

const MAX_ROWS = 5;

function formatAmount(value, currency) {
  const n = Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return currency ? `${n} ${currency}` : n;
}

/** Validate the untrusted (LLM-copied) spec; returns clean rows or null. */
export function parseChartSpec(raw) {
  let spec;
  try {
    spec = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return null;
  }
  if (!spec || !["bar", "donut"].includes(spec.type) || !Array.isArray(spec.data)) return null;
  const rows = spec.data
    .map((d) => ({ label: String(d?.label ?? ""), value: Number(d?.value) }))
    .filter((d) => d.label && Number.isFinite(d.value) && d.value > 0);
  if (rows.length === 0) return null;
  rows.sort((a, b) => b.value - a.value);
  return {
    type: spec.type,
    title: spec.title ? String(spec.title) : "",
    currency: spec.currency ? String(spec.currency) : "",
    rows,
  };
}

function Bars({ rows, currency }) {
  const [active, setActive] = useState(null);
  const max = rows[0].value;
  return (
    <ul className={styles.bars}>
      {rows.slice(0, MAX_ROWS).map((r, i) => (
        <li
          key={r.label}
          className={`${styles.barRow} ${active === i ? styles.barRowActive : ""}`}
          onMouseEnter={() => setActive(i)}
          onMouseLeave={() => setActive(null)}
        >
          <span className={styles.barLabel} title={r.label}>
            {r.label}
          </span>
          <span className={styles.barTrack}>
            <span
              className={styles.barFill}
              style={{ width: `${Math.max((r.value / max) * 100, 2)}%`, background: BAR_COLOR }}
            />
          </span>
          <span className={styles.barValue}>{formatAmount(r.value, currency)}</span>
        </li>
      ))}
    </ul>
  );
}

function Donut({ rows, currency }) {
  const [active, setActive] = useState(null);
  const shown = rows.slice(0, MAX_ROWS);
  const rest = rows.slice(MAX_ROWS).reduce((sum, r) => sum + r.value, 0);
  const slices = rest > 0 ? [...shown, { label: "Other", value: rest }] : shown;
  const total = slices.reduce((sum, r) => sum + r.value, 0);

  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <div className={styles.donutWrap}>
      <svg viewBox="0 0 140 140" className={styles.donut} role="img" aria-label="Donut chart">
        {slices.map((s, i) => {
          const len = (s.value / total) * circumference;
          const gap = slices.length > 1 ? 2 : 0; // 2px surface gap between fills
          const el = (
            <circle
              key={s.label}
              cx="70"
              cy="70"
              r={radius}
              fill="none"
              stroke={s.label === "Other" ? OTHER_COLOR : DONUT_COLORS[i % DONUT_COLORS.length]}
              strokeWidth={active === i ? 22 : 18}
              strokeDasharray={`${Math.max(len - gap, 0)} ${circumference - Math.max(len - gap, 0)}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 70 70)"
              onMouseEnter={() => setActive(i)}
              onMouseLeave={() => setActive(null)}
            >
              <title>{`${s.label}: ${formatAmount(s.value, currency)} (${Math.round((s.value / total) * 100)}%)`}</title>
            </circle>
          );
          offset += len;
          return el;
        })}
        <text x="70" y="66" textAnchor="middle" className={styles.donutTotalLabel}>
          Total
        </text>
        <text x="70" y="82" textAnchor="middle" className={styles.donutTotal}>
          {Math.round(total).toLocaleString()}
        </text>
      </svg>
      <ul className={styles.legend}>
        {slices.map((s, i) => (
          <li
            key={s.label}
            className={`${styles.legendRow} ${active === i ? styles.barRowActive : ""}`}
            onMouseEnter={() => setActive(i)}
            onMouseLeave={() => setActive(null)}
          >
            <span
              className={styles.swatch}
              style={{ background: s.label === "Other" ? OTHER_COLOR : DONUT_COLORS[i % DONUT_COLORS.length] }}
            />
            <span className={styles.legendLabel} title={s.label}>
              {s.label}
            </span>
            <span className={styles.legendValue}>
              {formatAmount(s.value, currency)} · {Math.round((s.value / total) * 100)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DataTable({ rows, currency }) {
  return (
    <table className={styles.table}>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <td>{r.label}</td>
            <td className={styles.num}>{formatAmount(r.value, currency)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ChatChart({ spec }) {
  const [asTable, setAsTable] = useState(false);
  const chart = parseChartSpec(spec);
  if (!chart) return null;

  return (
    <figure className={styles.card}>
      <figcaption className={styles.header}>
        <span className={styles.title}>{chart.title}</span>
        <button type="button" className={styles.toggle} onClick={() => setAsTable((v) => !v)}>
          {asTable ? "View chart" : "View table"}
        </button>
      </figcaption>
      {asTable ? (
        <DataTable rows={chart.rows} currency={chart.currency} />
      ) : chart.type === "donut" ? (
        <Donut rows={chart.rows} currency={chart.currency} />
      ) : (
        <Bars rows={chart.rows} currency={chart.currency} />
      )}
    </figure>
  );
}
