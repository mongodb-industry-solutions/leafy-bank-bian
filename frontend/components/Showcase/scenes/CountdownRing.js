"use client";

import styles from "../Showcase.module.css";

const R = 44;
const CIRC = 2 * Math.PI * R;

/**
 * Seconds left in the statement window, drawn as a draining ring so a wait reads as a wait.
 * `text` replaces the centre figure (default `${left}s`) when `left` is in another unit.
 */
export default function CountdownRing({ left, total, label, text }) {
  const fraction = Math.max(0, Math.min(1, left / total));
  const figure = text ?? `${left}s`;
  return (
    <div className={styles.ringWrap}>
      <svg width="104" height="104" viewBox="0 0 104 104" role="img" aria-label={text ? `${text} left` : `${left} seconds left`}>
        <circle cx="52" cy="52" r={R} fill="none" stroke="#e8edeb" strokeWidth="8" />
        <circle
          cx="52" cy="52" r={R} fill="none" stroke="#ffc010" strokeWidth="8" strokeLinecap="round"
          strokeDasharray={CIRC} strokeDashoffset={CIRC * (1 - fraction)}
          transform="rotate(-90 52 52)" style={{ transition: "stroke-dashoffset 1s linear" }}
        />
        <text x="52" y="58" textAnchor="middle" fontSize="22" fontWeight="600">{figure}</text>
      </svg>
      <span>{label}</span>
    </div>
  );
}
