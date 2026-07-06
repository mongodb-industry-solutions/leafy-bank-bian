import React, { useState } from "react";
import styles from "./GlMonitor.module.css";
import { PRESETS } from "@/lib/glMonitor/presets";
import { coreApi } from "@/lib/api/client";

// Initiate Transaction panel — ported from initiate.js. The POST now goes
// through the /api/backend proxy (coreApi) which routes PaymentOrderInitiation
// to the transactions service. After firing, the parent refreshes the columns.
export default function InitiatePanel({ onFired, onRefresh }) {
  const [collapsed, setCollapsed] = useState(false);
  const [showCustom, setShowCustom] = useState(false);
  const [busy, setBusy] = useState(null); // preset index or "custom" currently firing
  const [result, setResult] = useState(null); // { ok, text } | null

  const [custom, setCustom] = useState({
    customer: "CUST-00528224",
    debtor: "ACC-e0583b3b",
    creditor: "ACC-e0583b3c",
    amount: "50.00",
    type: "CREDIT_TRANSFER",
    rail: "INTERNAL",
    remittance: "",
  });
  const setField = (k, v) => setCustom((c) => ({ ...c, [k]: v }));

  async function fireTransaction(payload) {
    setResult({ loading: true });
    const { data, error } = await coreApi("PaymentOrderInitiation/Initiate", { method: "POST", body: payload });
    if (error) {
      setResult({ ok: false, text: `Error: ${error}` });
      return;
    }
    const pid = data.paymentId || data.payment_id || data.id || "—";
    setResult({ ok: true, pid });
    setTimeout(() => onFired?.(), 4000);
  }

  async function firePreset(idx) {
    const p = PRESETS[idx];
    setBusy(idx);
    const payload = {
      customerId: p.customerId, type: p.type, rail: p.rail,
      debtor: { accountId: p.debtor }, creditor: { accountId: p.creditor },
      instructedAmount: p.amount, instructedCurrency: "USD",
    };
    if (p.remittance) payload.remittance = { unstructured: p.remittance };
    await fireTransaction(payload);
    setBusy(null);
  }

  async function fireCustom() {
    const amount = parseFloat(custom.amount);
    if (!custom.debtor.trim() || !custom.creditor.trim() || isNaN(amount) || amount <= 0) {
      alert("Debtor Account, Creditor Account, and a positive Amount are required.");
      return;
    }
    const payload = {
      customerId: custom.customer.trim() || "CUST-00528224",
      type: custom.type,
      rail: custom.rail,
      debtor: { accountId: custom.debtor.trim() },
      creditor: { accountId: custom.creditor.trim() },
      instructedAmount: amount, instructedCurrency: "USD",
    };
    if (custom.remittance.trim()) payload.remittance = { unstructured: custom.remittance.trim() };
    setBusy("custom");
    try { await fireTransaction(payload); } finally { setBusy(null); }
  }

  return (
    <div style={{ padding: "0 20px 12px" }}>
      <div className={styles.panel}>
        <div className={styles["panel-header"]}>
          <span className={styles["panel-title"]}>Initiate Transaction</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>
            POST to transactions service · watch the pipeline below react live
          </span>
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
            <button className={styles.btn} onClick={onRefresh}>↻ Refresh All</button>
            <button className={`${styles.btn} ${styles["btn-primary"]} ${styles["btn-sm"]}`} onClick={onRefresh}>Load Data</button>
          </div>
          <button
            className={styles["panel-toggle-btn"]}
            style={{ marginLeft: 8, transform: collapsed ? "rotate(-90deg)" : "" }}
            onClick={() => setCollapsed((c) => !c)}
            aria-label="Toggle panel"
          >▾</button>
        </div>

        {!collapsed && (
          <div className={styles["panel-body"]}>
            <div style={{ marginBottom: 4 }}>
              <div className={styles["field-label"]} style={{ marginBottom: 8 }}>
                Real-time presets <span style={{ textTransform: "none", fontWeight: 400, color: "var(--text-xs)" }}>— rail WIRE/INTERNAL</span>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 14 }}>
                {PRESETS.map((p, i) => [p, i]).filter(([p]) => p.mode === "REALTIME").map(([p, i]) => (
                  <button
                    key={i}
                    className={styles["preset-btn"]}
                    disabled={busy === i}
                    onClick={() => firePreset(i)}
                  >💰 {p.label}{busy === i ? " …" : ""}</button>
                ))}
              </div>

              <div className={styles["field-label"]} style={{ marginBottom: 8 }}>
                Batch presets <span style={{ textTransform: "none", fontWeight: 400, color: "var(--text-xs)" }}>— rail ACH/VENMO/PAYPAL</span>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {PRESETS.map((p, i) => [p, i]).filter(([p]) => p.mode === "BATCH").map(([p, i]) => (
                  <button
                    key={i}
                    className={styles["preset-btn"]}
                    disabled={busy === i}
                    onClick={() => firePreset(i)}
                  >🏦 {p.label}{busy === i ? " …" : ""}</button>
                ))}
                <button
                  className={`${styles["preset-btn"]} ${styles["preset-btn-action"]}`}
                  onClick={() => setShowCustom((s) => !s)}
                >{showCustom ? "✕ Custom transaction" : "✏️ Custom transaction"}</button>
                <button className={`${styles["preset-btn"]} ${styles["preset-btn-muted"]}`} disabled title="Coming soon">⏳ Bulk transactions</button>
              </div>
            </div>

            {showCustom && (
              <div style={{ borderTop: "1px solid var(--border)", margin: "14px 0", paddingTop: 14 }}>
                <div className={styles["field-label"]} style={{ marginBottom: 10 }}>Custom transaction</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end" }}>
                  <div style={{ minWidth: 155, flex: 1.2 }}>
                    <div className={styles["field-label"]}>Customer ID</div>
                    <input className={styles["form-input"]} list="glm-customer-list" placeholder="CUST-…" value={custom.customer} onChange={(e) => setField("customer", e.target.value)} />
                    <datalist id="glm-customer-list">
                      <option value="CUST-00528224">Grace</option>
                      <option value="CUST-17352703">Monet</option>
                    </datalist>
                  </div>
                  <div style={{ minWidth: 155, flex: 1.5 }}>
                    <div className={styles["field-label"]}>Debtor Account (from)</div>
                    <input className={styles["form-input"]} list="glm-account-list" placeholder="ACC-…" value={custom.debtor} onChange={(e) => setField("debtor", e.target.value)} />
                  </div>
                  <div style={{ minWidth: 155, flex: 1.5 }}>
                    <div className={styles["field-label"]}>Creditor Account (to)</div>
                    <input className={styles["form-input"]} list="glm-account-list" placeholder="ACC-…" value={custom.creditor} onChange={(e) => setField("creditor", e.target.value)} />
                    <datalist id="glm-account-list">
                      <option value="ACC-e0583b3b">Grace primary</option>
                      <option value="ACC-e0583b3f">Grace secondary</option>
                      <option value="ACC-e0583b3c">Monet primary</option>
                      <option value="ACC-8c8097a4">Monet secondary</option>
                    </datalist>
                  </div>
                  <div style={{ minWidth: 90, flex: 0.7 }}>
                    <div className={styles["field-label"]}>Amount (USD)</div>
                    <input className={styles["form-input"]} type="number" min="0.01" step="0.01" value={custom.amount} onChange={(e) => setField("amount", e.target.value)} />
                  </div>
                  <div style={{ minWidth: 165, flex: 1.2 }}>
                    <div className={styles["field-label"]}>Type</div>
                    <select className={styles["form-input"]} value={custom.type} onChange={(e) => setField("type", e.target.value)}>
                      <option value="CREDIT_TRANSFER">CREDIT_TRANSFER</option>
                      <option value="INTRABANK_TRANSFER">INTRABANK_TRANSFER</option>
                    </select>
                  </div>
                  <div style={{ minWidth: 100, flex: 0.8 }}>
                    <div className={styles["field-label"]}>Rail</div>
                    <select className={styles["form-input"]} value={custom.rail} onChange={(e) => setField("rail", e.target.value)}>
                      <option value="INTERNAL">INTERNAL</option>
                      <option value="ACH">ACH</option>
                      <option value="WIRE">WIRE</option>
                    </select>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 10, marginTop: 10, alignItems: "flex-end" }}>
                  <div style={{ flex: 1 }}>
                    <div className={styles["field-label"]}>Remittance (optional)</div>
                    <input className={styles["form-input"]} placeholder="Payment description" value={custom.remittance} onChange={(e) => setField("remittance", e.target.value)} />
                  </div>
                  <button className={`${styles.btn} ${styles["btn-primary"]}`} disabled={busy === "custom"} onClick={fireCustom}>
                    {busy === "custom" ? "Firing…" : "Fire Transaction"}
                  </button>
                </div>
              </div>
            )}

            {result && (
              <div style={{ marginTop: 12 }}>
                {result.loading ? (
                  <div className={styles["empty-state"]}>Sending…</div>
                ) : result.ok ? (
                  <div style={{ background: "var(--green-bg)", color: "var(--green-text)", borderRadius: 6, padding: "10px 14px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    <span style={{ fontWeight: 700 }}>Initiated:</span>
                    <span style={{ fontFamily: "monospace", fontSize: 12 }}>{result.pid}</span>
                    <span style={{ fontSize: 11, color: "var(--green-text)" }}>Refreshing in 4s</span>
                  </div>
                ) : (
                  <div className={styles["error-state"]}>{result.text}</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
