"use client";

// Custom status pill for the Payments Analyst surface — replaces @leafygreen-ui/badge here.
// Per research §3.4: 24px tall, 12px uppercase 600, 1px solid border in the status color,
// light2 background + dark2 text, pill radius. Scoped to this route's CSS module so it cannot
// leak; the rest of the app (TransactionsTable, GL monitor) keeps LG Badge.
import styles from "./PaymentsWorkflow.module.css";
import { pillFamily } from "@/lib/paymentsWorkflow/status";

const FAMILY_CLASS = {
  green: styles.pillGreen,
  blue: styles.pillBlue,
  yellow: styles.pillYellow,
  red: styles.pillRed,
  gray: styles.pillGray,
};

/**
 * `status` — a lifecycle state string; family is derived via pillFamily.
 * `family` — pass directly when the color is not a status (e.g. a "green"/"red" verdict or
 *            a neutral tag). Overrides `status`.
 * Label falls back to `children`, then `status`.
 */
export default function StatusPill({ status, family, label, children }) {
  const fam = family ?? pillFamily(status);
  const text = label ?? children ?? status ?? "—";
  return (
    <span className={`${styles.pill} ${FAMILY_CLASS[fam] ?? styles.pillGray}`}>
      {text}
    </span>
  );
}
