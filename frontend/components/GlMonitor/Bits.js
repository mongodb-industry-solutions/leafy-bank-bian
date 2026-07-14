// Shared render primitives for the GL monitor (ported from utils.js chip()/kv()
// and batch.js batchChip()). Kept in one place so columns, detail and trace
// views render identically.
import React from "react";
import styles from "./GlMonitor.module.css";
import { chipVariant } from "@/lib/glMonitor/format";

export function Chip({ status }) {
  const cls = chipVariant(status);
  return <span className={`${styles.chip} ${styles[`chip-${cls}`]}`}>{status || "—"}</span>;
}

// Chip with an explicit variant + label (e.g. current/last/ok/mismatch/dr/cr).
export function VariantChip({ variant, children }) {
  return <span className={`${styles.chip} ${styles[`chip-${variant}`]}`}>{children}</span>;
}

// Double-entry balance indicator — "DR = CR" when legs match, else a warning.
export function BalanceChip({ balanced }) {
  return (
    <VariantChip variant={balanced ? "ok" : "mismatch"}>
      {balanced ? "DR = CR" : "DR ≠ CR"}
    </VariantChip>
  );
}

// Key/value table — pairs is an array of [label, value]; value may be a node.
export function KvTable({ pairs }) {
  return (
    <table className={styles["kv-table"]}>
      <tbody>
        {pairs.map(([k, v], i) => (
          <tr key={i}>
            <td>{k}</td>
            <td>{v ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
