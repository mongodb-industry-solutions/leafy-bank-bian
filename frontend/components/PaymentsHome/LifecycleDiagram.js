"use client";

import Icon from "@leafygreen-ui/icon";
import { Body, H3 } from "@leafygreen-ui/typography";
import styles from "./LifecycleDiagram.module.css";

// Grid columns are 1-based over the nine stages; `cols` is a CSS grid-column value.
const PERSONAS = [
  { name: "Bank Customer", role: "Initiates and monitors the payment", cols: "1 / 3", tone: "green" },
  { name: "Payments Operations", role: "Monitors lifecycle, exceptions and resolutions", cols: "3 / 6", tone: "green" },
  { name: "Payments Analyst", role: "Monitors execution, settlement and reconciliation", cols: "6 / 8", tone: "blue" },
  { name: "Finance Operator", role: "Reviews accounting records, batches and posting", cols: "8 / 10", tone: "purple" },
];

const STAGES = [
  { n: 1, title: "Payment Initiation", sub: "Create the payment instruction", tone: "green", icon: "Edit",
    items: ["Capture debtor & creditor", "Idempotency key", "Snapshot parties"] },
  { n: 2, title: "Authentication & Authorization", sub: "Verify customer and permissions", tone: "green", icon: "Key",
    items: ["Verify caller", "Check entitlements", "Step-up if needed"] },
  { n: 3, title: "Validate & Enrich", sub: "Validate the instruction and enrich data", tone: "green", icon: "Sparkle", agent: true,
    items: ["Enrich beneficiary", "Validate bank details", "Check sanctions", "Estimate fees / FX"] },
  { n: 4, title: "Payment Orchestration", sub: "Score fraud, pick the rail, prepare ISO 20022", tone: "green", icon: "Lock",
    items: ["Fraud decision", "Choose network", "Routing snapshot"] },
  { n: 5, title: "Payment Execution", sub: "Send and receive confirmations", tone: "green", icon: "ArrowRight",
    items: ["Build pacs.008", "Send to correspondent", "Receive pacs.002"] },
  { n: 6, title: "Accounting & Posting", sub: "Ledger events, sub-ledger, GL", tone: "blue", icon: "Diagram3",
    flow: ["Ledger Events (append-only)", "Sub-ledger (near real-time)", "General Ledger (batched)"] },
  { n: 7, title: "Clearing & Settlement", sub: "Settlement and position updates", tone: "blue", icon: "University",
    items: ["Settlement position", "Value date", "Settlement ledger event"] },
  { n: 8, title: "Reconciliation", sub: "Match across systems and find discrepancies", tone: "purple", icon: "Sparkle", agent: true,
    items: ["Compare payment, rail, GL", "Identify mismatches", "Analyze root cause", "Recommend resolution"] },
  { n: 9, title: "Exceptions / Returns", sub: "Investigate, resolve and complete", tone: "purple", icon: "Warning",
    items: ["Return / reversal", "Repair & resubmit", "Manual investigation", "Resolved"] },
];

const CANONICAL = [
  ["Canonical Payment", "paymentId"],
  ["Payment Type Extension", "wireDetails / internalDetails"],
  ["Lifecycle State", "status and history"],
  ["Reference Data", "banks, identifiers, fees, FX"],
  ["Operational Data", "settlement, reconciliation, exceptions"],
];

const AGENTS = [
  { name: "Payment Enrichment Agent", at: "Stage 3", tone: "green", cols: "3 / 6",
    tools: [["Database", "Reference data lookup"], ["Edit", "Payment update"], ["CheckmarkWithCircle", "Validation"]] },
  { name: "Reconciliation Agent", at: "Stage 8", tone: "purple", cols: "7 / 10",
    tools: [["MagnifyingGlass", "Payment trace lookup"], ["Warning", "Reconciliation analysis"], ["Person", "Exception & resolution"]] },
];

const COLLECTIONS = [
  ["payments", "Canonical payment and lifecycle"],
  ["routingSnapshots", "Beneficiaries, banks, routing"],
  ["ledgerEvents", "Append-only time series"],
  ["subLedgerEntries", "Operational balances"],
  ["journalEntries", "General ledger"],
  ["settlementPositions", "External confirmations"],
  ["reconciliationItems", "Three-way match results"],
  ["exceptions", "Issues, returns, resolutions"],
];

function LayerLabel({ icon, title, body }) {
  return (
    <div className={styles.layerLabel}>
      <Icon glyph={icon} size="xlarge" className={styles.layerIcon} />
      <div>
        <H3 as="p" className={styles.layerTitle}>{title}</H3>
        <Body className={styles.muted}>{body}</Body>
      </div>
    </div>
  );
}

export default function LifecycleDiagram() {
  return (
    <div className={styles.scroller}>
      <div className={styles.diagram}>
        <div className={styles.head}>
          <div>
            <H3 as="h2" className={styles.headTitle}>Payments Lifecycle: Wire (ISO 20022)</H3>
            <Body className={styles.muted}>
              From customer initiation to reconciliation, with a canonical data layer and agentic AI
            </Body>
          </div>
        </div>

        <div className={styles.nine}>
          {PERSONAS.map((p) => (
            <div key={p.name} className={`${styles.persona} ${styles[p.tone]}`} style={{ gridColumn: p.cols }}>
              <Icon glyph="Person" size="large" />
              <div>
                <strong>{p.name}</strong>
                <span>{p.role}</span>
              </div>
            </div>
          ))}

          {STAGES.map((s) => (
            <div key={s.n} className={`${styles.stage} ${styles[s.tone]}`}>
              <div className={styles.chevron}>
                <span className={styles.num}>{s.n}</span>
                <strong className={styles.stageTitle}>
                  {s.title}
                  {s.agent && <span className={styles.aiTag}>Agentic AI</span>}
                </strong>
                <span className={styles.stageSub}>{s.sub}</span>
              </div>
              <div className={styles.stageBody}>
                <Icon glyph={s.icon} size="large" className={styles.stageIcon} />
                {s.items && (
                  <ul className={styles.items}>
                    {s.items.map((i) => (
                      <li key={i}><Icon glyph="Checkmark" size="small" /> {i}</li>
                    ))}
                  </ul>
                )}
                {s.flow && (
                  <div className={styles.flow}>
                    {s.flow.map((f, i) => (
                      <div key={f}>
                        {i > 0 && <Icon glyph="ArrowDown" size="small" className={styles.flowArrow} />}
                        <div className={styles.flowBox}><Icon glyph="Database" size="small" /> {f}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        <div className={`${styles.layer} ${styles.green}`}>
          <LayerLabel icon="Database" title="Canonical Payments Data Layer" body="One payment model across rails (wires, ACH, cards)" />
          <div className={styles.pills}>
            {CANONICAL.map(([t, d], i) => (
              <div key={t} className={styles.pillWrap}>
                {i > 0 && <Icon glyph="ChevronRight" className={styles.pillArrow} />}
                <div className={styles.pill}><strong>{t}</strong><span>{d}</span></div>
              </div>
            ))}
          </div>
        </div>

        <div className={`${styles.layer} ${styles.neutral}`}>
          <LayerLabel icon="Wizard" title="Agentic AI Support" body="Targeted agents that improve efficiency and reduce manual work" />
          <div className={styles.agents}>
            {AGENTS.map((a) => (
              <div key={a.name} className={`${styles.agent} ${styles[a.tone]}`} style={{ gridColumn: a.cols }}>
                <strong><Icon glyph="Sparkle" /> {a.name} <span className={styles.muted}>({a.at})</span></strong>
                <div className={styles.tools}>
                  {a.tools.map(([g, t]) => (
                    <span key={t} className={styles.tool}><Icon glyph={g} /> {t}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className={`${styles.layer} ${styles.forest}`}>
          <LayerLabel icon="Database" title="Enterprise Data & Systems (MongoDB Atlas)" body="Event-driven architecture with full traceability" />
          <div className={styles.collections}>
            {COLLECTIONS.map(([c, d]) => (
              <div key={c} className={styles.coll}>
                <Icon glyph="Database" />
                <div><code>{c}</code><span>{d}</span></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
