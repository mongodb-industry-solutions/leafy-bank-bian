"use client";

import Icon from "@leafygreen-ui/icon";
import { Body } from "@leafygreen-ui/typography";
import styles from "../Showcase.module.css";

/** Act 1: three checklist rows that tick as the one-click setup progresses. */
export default function SetupScene({ init, payment }) {
  const rows = [
    { label: "Sent", done: !!init, meta: init ? `${init.paymentId} · ${init.status}` : "" },
    { label: "Settled", done: ["SETTLED", "COMPLETED"].includes(payment?.status), meta: payment?.status || "" },
    {
      label: "Booked",
      done: payment?.lifecycle?.postingStatus === "POSTED",
      meta: "Journaled to the general ledger",
    },
  ];
  return (
    <div className={styles.section}>
      <ul className={styles.checklist}>
        {rows.map((r) => (
          <li key={r.label} className={`${styles.checkRow} ${r.done ? styles.checkDone : ""}`}>
            <Icon glyph={r.done ? "Checkmark" : "Ellipsis"} size={20} />
            <Body as="span">{r.label}</Body>
            {r.done && r.meta && <Body as="span" className={`${styles.muted} ${styles.mono}`}>{r.meta}</Body>}
          </li>
        ))}
      </ul>
      <Body className={styles.muted}>Lifecycle stages 1-7 complete. Open the payment under Payments to see them.</Body>
    </div>
  );
}
