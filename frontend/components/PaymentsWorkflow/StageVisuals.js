// Small stage-specific visuals: who pays whom (stage 1), the corridor decision and FX provenance (stage 3), the cut-off
// clock (stage 4), how a payment becomes ledger entries (stage 6) and how the expected
// settlement compares with what was booked (stage 7). Each reads only fields the backend
// already stores, and renders nothing when they are absent.

import { fmtAmount, fmtWhen } from "@/lib/paymentsWorkflow/status";
import { settlementDelta } from "./stageContent";
import styles from "./StageVisuals.module.css";

function Node({ label, detail, done }) {
  return (
    <div className={`${styles.node} ${done ? styles.nodeDone : styles.nodePending}`}>
      <span className={styles.nodeLabel}>{label}</span>
      <span className={styles.nodeDetail}>{detail}</span>
    </div>
  );
}

function Hop({ label, async: isAsync }) {
  return (
    <div className={styles.hop} aria-hidden="true">
      <span className={`${styles.hopLine} ${isAsync ? styles.hopAsync : ""}`} />
      <span className={styles.hopLabel}>{label}</span>
    </div>
  );
}

/** Payment, transaction, ledger event, sub-ledger entries, journal; async hops drawn dashed. */
export function PostingChain({ payment, trace }) {
  const events = trace?.allLedgerEvents?.length || 0;
  const entries = trace?.subLedgerEntries?.length || 0;
  const journals = trace?.allJournalEntries?.length || 0;
  return (
    <div className={styles.chain} role="group" aria-label="How the payment reaches the ledger">
      <Node label="Payment" detail={payment?.paymentId || "—"} done={!!payment} />
      <Hop label="same ACID transaction" />
      <Node label="Transaction" detail={trace?.transaction ? "debited" : "pending"} done={!!trace?.transaction} />
      <Hop label="change stream" async />
      <Node label="Ledger event" detail={events ? `${events} event${events > 1 ? "s" : ""}` : "pending"} done={events > 0} />
      <Hop label="GL batch" async />
      <Node label="Sub-ledger" detail={entries ? `${entries} entries` : "pending"} done={entries > 0} />
      <Hop label="same ACID transaction" />
      <Node label="Journal" detail={journals ? `${journals} posted` : "pending"} done={journals > 0} />
    </div>
  );
}

/** Instructed, expected at settlement, booked by the correspondent, and the gap between them. */
export function FundsFlow({ payment, position }) {
  const ccy = position?.currency || payment?.currency;
  const result = settlementDelta(position);
  const expected = position?.expectedAmount ?? position?.grossAmount;
  const short = result?.delta > 0;
  return (
    <div className={styles.chain} role="group" aria-label="Funds flow">
      <Node label="Instructed" detail={fmtAmount(payment?.amount, payment?.currency)} done={!!payment} />
      <Hop label="sent over the rail" />
      <Node label="Expected" detail={expected != null ? fmtAmount(expected, ccy) : "—"} done={expected != null} />
      <Hop label="correspondent statement" async />
      <Node
        label="Booked"
        detail={result ? fmtAmount(result.actual, ccy) : "awaiting statement"}
        done={!!result}
      />
      {result && (
        <div className={`${styles.deltaChip} ${short ? styles.deltaShort : styles.deltaMatch}`}>
          {short
            ? `Short by ${fmtAmount(result.delta, ccy)}`
            : result.delta < 0
              ? `Over by ${fmtAmount(-result.delta, ccy)}`
              : "Matches"}
        </div>
      )}
    </div>
  );
}

const CORRIDORS = [
  ["domestic-same-bank", "Domestic, same bank", "The beneficiary account is held at this bank."],
  ["domestic-different-bank", "Domestic, different bank", "The beneficiary bank is in this bank's country."],
  ["cross-border", "Cross-border", "The beneficiary bank is in another country."],
];

/** The three corridor categories, with the one stage 3 determined highlighted. */
export function CategoryTable({ payment }) {
  const determined = payment?.validation?.determinedCategory;
  if (!determined) return null;
  return (
    <div className={styles.table} role="table" aria-label="Corridor category">
      {CORRIDORS.map(([key, label, rule]) => (
        <div
          key={key}
          role="row"
          className={`${styles.tableRow} ${key === determined ? styles.tableRowOn : ""}`}
        >
          <span role="cell" className={styles.tableMark}>{key === determined ? "●" : "○"}</span>
          <span role="cell" className={styles.tableLabel}>{label}</span>
          <span role="cell" className={styles.tableRule}>{rule}</span>
        </div>
      ))}
    </div>
  );
}

/** Where the FX rate came from: pair, rate, source, quote and when it was taken. */
export function FxProvenance({ payment }) {
  const fx = payment?.fx;
  if (!fx?.fxRate) return null;
  const rows = [
    ["Pair", [fx.sourceCurrency, fx.targetCurrency].filter(Boolean).join(" → ")],
    ["Rate", fx.fxRate],
    ["Source", fx.rateSource],
    ["Quote", fx.quoteId],
    ["Taken", fx.rateTimestamp ? fmtWhen(fx.rateTimestamp) : null],
  ].filter(([, v]) => v != null && v !== "");
  return (
    <dl className={styles.facts} aria-label="FX provenance">
      {rows.map(([label, value]) => (
        <div className={styles.fact} key={label}>
          <dt className={styles.factLabel}>{label}</dt>
          <dd className={styles.factValue}>{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

const easternHour = (when) => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "numeric", hour12: false,
  }).formatToParts(new Date(when));
  const get = (t) => Number(parts.find((p) => p.type === t)?.value);
  return (get("hour") % 24) + get("minute") / 60;
};

const hhmm = (h) =>
  `${String(Math.floor(h)).padStart(2, "0")}:${String(Math.round((h % 1) * 60)).padStart(2, "0")}`;

/** A 24-hour Eastern bar: when the routing decision was made against the network's cut-off. */
export function CutoffClock({ snapshot }) {
  if (snapshot?.cutoffHourET == null) return null;
  const cutoff = Number(snapshot.cutoffHourET);
  const decided = snapshot.decidedAt ? easternHour(snapshot.decidedAt) : null;
  const pct = (h) => `${(h / 24) * 100}%`;
  return (
    <div className={styles.clock} role="group" aria-label="Cut-off clock">
      <div className={styles.clockBar}>
        <span className={styles.clockOpen} style={{ width: pct(cutoff) }} />
        <span className={styles.clockCutoff} style={{ left: pct(cutoff) }} />
        {decided != null && (
          <span
            className={`${styles.clockDecided} ${snapshot.withinCutoff ? "" : styles.clockLate}`}
            style={{ left: pct(decided) }}
          />
        )}
      </div>
      <div className={styles.clockLegend}>
        <span>{snapshot.clearingNetwork || "Network"} cut-off {hhmm(cutoff)} ET</span>
        {decided != null && <span>decided {hhmm(decided)} ET</span>}
        <span>
          {snapshot.withinCutoff
            ? "within cut-off"
            : `past cut-off${snapshot.valueDate ? ` — value date ${snapshot.valueDate}` : ""}`}
        </span>
      </div>
    </div>
  );
}

const initials = (name) =>
  String(name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");

function PartyBox({ role, party, external }) {
  const account = party?.accountNo ? `····${String(party.accountNo).slice(-4)}` : null;
  const bank = [party?.bankName, party?.bic, party?.bankCountry].filter(Boolean);
  return (
    <div className={styles.party}>
      <div className={styles.partyTop}>
        <span className={styles.avatar} aria-hidden="true">{initials(party?.name)}</span>
        <div className={styles.partyId}>
          <span className={styles.partyRole}>
            {role}
            {external && <span className={styles.partyTag}>external</span>}
          </span>
          <span className={styles.partyName}>{party?.name || "—"}</span>
        </div>
      </div>
      <dl className={styles.partyLines}>
        {bank.length > 0 && (
          <div>
            <dt>Bank</dt>
            <dd>{bank.join(" · ")}</dd>
          </div>
        )}
        {account && (
          <div>
            <dt>Account</dt>
            <dd className={styles.mono}>
              {account}
              {party?.accountType ? ` · ${party.accountType}` : ""}
            </dd>
          </div>
        )}
        {party?.accountId && (
          <div>
            <dt>Account ID</dt>
            <dd className={styles.mono}>{party.accountId}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}

/** Debtor, the amount and rail travelling between them, creditor. Stacks when narrow. */
export function PartyFlow({ payment }) {
  const wireType = payment?.wireDetails?.wireType;
  return (
    <div className={styles.flowFrame}>
      <div className={styles.flow}>
        <PartyBox role="Payer" party={payment?.debtor} />
        <div className={styles.flowLink}>
          <span className={styles.flowAmount}>{fmtAmount(payment?.amount, payment?.currency)}</span>
          <span className={styles.flowArrow} aria-hidden="true" />
          <span className={styles.flowRail}>
            {[payment?.rail, wireType].filter(Boolean).join(" · ")}
          </span>
        </div>
        <PartyBox role="Beneficiary" party={payment?.creditor} external />
      </div>
    </div>
  );
}
