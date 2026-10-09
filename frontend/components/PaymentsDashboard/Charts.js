"use client";

// Dependency-free chart primitives for the Operations dashboard. Donuts and ranked bars are
// plain SVG/CSS: they render on the server, cost no bundle, and need no tooltips. The
// axis charts (stacked volume, trend) live in AxisCharts.js and use Recharts.
import styles from "./PaymentsDashboard.module.css";

const RADIUS = 42;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** `segments`: [{label, value, color}]. Center shows `total` and `caption`. */
export function Donut({ segments, total, caption = "Total", size = 168 }) {
  const sum = segments.reduce((acc, s) => acc + s.value, 0);
  let offset = 0;
  return (
    <div className={styles.donut} style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" role="img" aria-label={`${caption}: ${total}`}>
        <circle cx="50" cy="50" r={RADIUS} fill="none" stroke="#e8edeb" strokeWidth="12" />
        {sum > 0 &&
          segments
            .filter((s) => s.value > 0)
            .map((s) => {
              const length = (s.value / sum) * CIRCUMFERENCE;
              const dash = `${length} ${CIRCUMFERENCE - length}`;
              const el = (
                <circle
                  key={s.label}
                  cx="50"
                  cy="50"
                  r={RADIUS}
                  fill="none"
                  stroke={s.color}
                  strokeWidth="12"
                  strokeDasharray={dash}
                  strokeDashoffset={-offset}
                  transform="rotate(-90 50 50)"
                />
              );
              offset += length;
              return el;
            })}
      </svg>
      <div className={styles.donutCenter}>
        <strong>{total}</strong>
        <span>{caption}</span>
      </div>
    </div>
  );
}

/** Legend rows: dot, label, count, share. */
export function Legend({ segments }) {
  const sum = segments.reduce((acc, s) => acc + s.value, 0);
  return (
    <ul className={styles.legend}>
      {segments.map((s) => (
        <li key={s.label}>
          <span className={styles.dot} style={{ background: s.color }} />
          <span className={styles.legendLabel}>{s.label}</span>
          <span className={styles.legendValue}>{s.value.toLocaleString()}</span>
          <span className={styles.legendShare}>{sum ? Math.round((s.value / sum) * 100) : 0}%</span>
        </li>
      ))}
    </ul>
  );
}

/** Horizontal ranked bars. `rows`: [{label, value, color?}]. */
export function BarList({ rows, color = "#00a35c", showShare = false }) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  const sum = rows.reduce((acc, r) => acc + r.value, 0);
  return (
    <ul className={styles.barList}>
      {rows.map((r) => (
        <li key={r.label}>
          <span className={styles.barLabel}>{r.label}</span>
          <span className={styles.barTrack}>
            <span
              className={styles.barFill}
              style={{ width: `${(r.value / max) * 100}%`, background: r.color ?? color }}
            />
          </span>
          <span className={styles.barValue}>{r.value.toLocaleString()}</span>
          {showShare && <span className={styles.legendShare}>{sum ? Math.round((r.value / sum) * 100) : 0}%</span>}
        </li>
      ))}
    </ul>
  );
}
