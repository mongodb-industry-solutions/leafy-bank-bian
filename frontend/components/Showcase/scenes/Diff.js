"use client";

import styles from "../Showcase.module.css";

const mark = (text, key) => (
  <mark key={key} className={styles.diffMark}>{text}</mark>
);

/** Same-length strings (amounts): mark the characters of `theirs` that differ from `ours`. */
export function PositionDiff({ ours, theirs }) {
  if (ours.length !== theirs.length) return <>{theirs}</>;
  return (
    <>
      {[...theirs].map((c, i) => (c === ours[i] ? c : mark(c, i)))}
    </>
  );
}

/**
 * References: the correspondent drops our prefix and re-keys the rest, so a positional diff
 * would flag everything. Keep the shared core (compared case-insensitively) plain and mark
 * what was added or lost around it.
 */
export function ReferenceDiff({ ours, theirs, side }) {
  const core = String(ours).split("-").pop();
  const at = String(theirs).toUpperCase().indexOf(core.toUpperCase());
  if (at < 0) return <>{side === "ours" ? ours : mark(theirs, "all")}</>;
  if (side === "ours") {
    const i = String(ours).indexOf(core);
    return <>{i > 0 && mark(ours.slice(0, i), "p")}{ours.slice(i)}</>;
  }
  const end = at + core.length;
  return (
    <>
      {at > 0 && mark(theirs.slice(0, at), "a")}
      {theirs.slice(at, end)}
      {end < theirs.length && mark(theirs.slice(end), "b")}
    </>
  );
}
