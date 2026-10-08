// Small stage-specific visuals: who pays whom (stage 1), the corridor decision and FX provenance (stage 3), the cut-off
// clock (stage 4), how a payment becomes ledger entries (stage 6) and how the expected
// settlement compares with what was booked (stage 7). Each reads only fields the backend
// already stores, and renders nothing when they are absent.

import Icon from "@leafygreen-ui/icon";
import Tooltip from "@leafygreen-ui/tooltip";
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
const INBOUND_CORRIDORS = [
  ["DOMESTIC", "Domestic", "The sending bank is in this bank's country."],
  ["CROSS_BORDER", "Cross-border", "The sending bank is in another country."],
];

export function CategoryTable({ payment }) {
  const determined = payment?.validation?.determinedCategory;
  if (!determined) return null;
  // Inbound compares the ORIGINATOR's bank country, so it has two outcomes, not three.
  const corridors = payment?.direction === "INBOUND" ? INBOUND_CORRIDORS : CORRIDORS;
  return (
    <div className={styles.table} role="table" aria-label="Corridor category">
      {corridors.map(([key, label, rule]) => (
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

function PartyBox({ role, party, tag }) {
  const account = party?.accountNo ? `····${String(party.accountNo).slice(-4)}` : null;
  const bank = [party?.bankName, party?.bic, party?.bankCountry].filter(Boolean);
  return (
    <div className={styles.party}>
      <div className={styles.partyTop}>
        <span className={styles.avatar} aria-hidden="true">{initials(party?.name)}</span>
        <div className={styles.partyId}>
          <span className={styles.partyRole}>
            {role}
            {tag && <span className={styles.partyTag}>{tag}</span>}
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

const TRAVEL_RULE =
  "Debtor and creditor are stored as point-in-time snapshots, not live references to a customer " +
  "or bank directory. The beneficiary may not be a Leafy Bank customer, and the Travel Rule " +
  "(FATF Recommendation 16) requires originator information to travel unchanged with the " +
  "payment, whatever happens to the customer record afterwards.";

function creditorTag(payment) {
  if (payment?.direction !== "INBOUND") return "external";
  const outcome = payment?.beneficiaryResolution?.matchOutcome;
  if (outcome === "MATCHED") return "confirmed";
  if (outcome === "PARTIAL") return "partial match";
  return "claimed, not yet confirmed";
}

/** Debtor, the amount and rail travelling between them, creditor, and the snapshot note. */
export function PartyFlow({ payment }) {
  const inbound = payment?.direction === "INBOUND";
  const wireType = payment?.wireDetails?.wireType;
  return (
    <div className={styles.flowFrame}>
      <div className={styles.flow}>
        <PartyBox role={inbound ? "Originator" : "Payer"} party={payment?.debtor} />
        <div className={styles.flowLink}>
          <span className={styles.flowAmount}>{fmtAmount(payment?.amount, payment?.currency)}</span>
          <span className={styles.flowArrow} aria-hidden="true" />
          <span className={styles.flowRail}>
            {[payment?.rail, wireType].filter(Boolean).join(" · ")}
          </span>
        </div>
        <PartyBox
          role={inbound ? "Claimed beneficiary" : "Beneficiary"}
          party={payment?.creditor}
          tag={creditorTag(payment)}
        />
      </div>
      <div className={styles.snapshotNote}>
        <Icon glyph="Lock" size={14} />
        <span>
          {inbound ? "Taken from the message" : "Frozen"}
          {payment?.initiatedAt ? ` at ${fmtWhen(payment.initiatedAt)}` : ""}. Debtor and Creditor are stored as point in time snapshots.
        </span>
        <Tooltip
          trigger={
            <button type="button" className={styles.noteButton} aria-label="Why the parties are frozen">
              <Icon glyph="InfoWithCircle" size={14} />
            </button>
          }
        >
          {TRAVEL_RULE}
        </Tooltip>
      </div>
    </div>
  );
}

const ENVELOPES = [
  ["WIRE", "Wire"],
  ["INTERNAL", "Internal"],
  ["ACH", "ACH"],
  ["CARD", "Card"],
];

/** Which rail envelope is populated; the others stay empty by design. */
export function EnvelopeChips({ rail }) {
  return (
    <div className={styles.envelopeChips} aria-label="Rail envelopes">
      {ENVELOPES.map(([key, label]) => (
        <span
          key={key}
          className={`${styles.envelopeChip} ${key === rail ? styles.envelopeChipOn : ""}`}
        >
          {key === rail ? "●" : "○"} {label}
        </span>
      ))}
    </div>
  );
}

/**
 * A progress track: one dot per state, joined by a line. Filled = the payment has been in the
 * state, ringed = the next one it moves to, hollow = further ahead. The note says what happens
 * next, under the track.
 */
export function StatesStrip({ seen, states, note }) {
  const nextIndex = states.findIndex(({ state }) => !seen.has(state));
  return (
    <div className={styles.track} aria-label="Lifecycle states">
      <ol className={styles.trackSteps}>
        {states.map(({ state, optional }, i) => {
          const done = seen.has(state);
          const kind = done ? styles.trackDone : i === nextIndex ? styles.trackNext : "";
          return (
            <li key={state} className={`${styles.trackStep} ${kind}`}>
              <span className={styles.trackDot} aria-hidden="true">{done ? "✓" : ""}</span>
              <span className={styles.trackLabel}>
                {state}
                {optional && <span className={styles.trackOptional}>only if held</span>}
              </span>
            </li>
          );
        })}
      </ol>
      {note && <div className={styles.trackNote}>{note}</div>}
    </div>
  );
}

/** Every state name the payment is, or has been, in: events plus the per-leg statuses. */
export function seenStates(payment) {
  const lc = payment?.lifecycle || {};
  const names = [
    ...(lc.events || []).map((e) => e.state),
    payment?.status,
    lc.postingStatus,
    lc.settlementStatus,
    lc.reconciliationStatus,
  ];
  return new Set(names.filter(Boolean).map((n) => String(n).toUpperCase()));
}

const S = (state, optional) => ({ state, optional });

// States each stage moves the payment through. A stage that moves nothing says so in its note.
const STAGE_STATES = {
  authentication: {
    OUTBOUND: { states: [S("INITIATED")], note: "unchanged here; a step-up holds the payment at INITIATED" },
    INBOUND: { states: [S("RECEIVED")], note: "unchanged here; NO_MATCH goes to stage 9" },
  },
  validation: {
    both: { states: [S("VALIDATED"), S("ENRICHED"), S("FINAL_VALIDATED")], note: "Stage 4 routes it next." },
  },
  authorization: {
    OUTBOUND: {
      states: [S("ROUTED"), S("MANUAL_FRAUD_REVIEW", true), S("AUTHORISED"), S("APPROVED")],
      note: "Stage 5 submits it next.",
    },
    INBOUND: {
      states: [S("FINAL_VALIDATED"), S("ACCEPTED")],
      note: "ACCEPTED is set when stage 5 sends the pacs.002",
    },
  },
  execution: {
    OUTBOUND: { states: [S("SUBMITTED"), S("IN_PROGRESS")], note: "Stage 6 posts it next." },
    INBOUND: { states: [S("ACCEPTED")], note: "set on transmission, before internal posting" },
  },
  "g:Accounting & Posting": { both: { states: [S("POSTED")], note: "lifecycle.postingStatus" } },
  "g:Clearing & Settlement": {
    both: { states: [S("PENDING"), S("SETTLED")], note: "lifecycle.settlementStatus" },
  },
  reconciliation: { both: { states: [S("SETTLED"), S("RECONCILED")], note: "terminal state" } },
};

/** The state chips for a stage, or nothing when the stage defines none. */
export function StageStates({ stageKey, payment }) {
  const entry = STAGE_STATES[stageKey];
  const def = entry?.[payment?.direction] || entry?.both;
  if (!def) return null;
  return <StatesStrip seen={seenStates(payment)} states={def.states} note={def.note} />;
}

/** Stage 1 sets DRAFT then INITIATED, or RECEIVED for an inbound wire. VALIDATED is stage 3's. */
export function StatePath({ events, inbound }) {
  const states = (inbound ? ["RECEIVED"] : ["DRAFT", "INITIATED"]).map((s) => S(s));
  const seen = new Set((events || []).map((e) => String(e.state || "").toUpperCase()));
  return <StatesStrip seen={seen} states={states} note="Stage 2 checks it without changing the state. Stage 3 moves it to VALIDATED." />;
}

/** Inbound intake order: the raw message is persisted before the payment is created. */
export function IntakeOrder() {
  return (
    <div className={styles.statePath} aria-label="Inbound intake order">
      <span className={styles.stateChip}>1 · raw pacs.008 stored (paymentMessages)</span>
      <span className={styles.statePathArrow} aria-hidden="true">→</span>
      <span className={styles.stateChip}>2 · payment created (payments)</span>
      <span className={styles.statePathNote}>the original message survives a parse failure</span>
    </div>
  );
}

/** BIAN service domains for the stage; the operation is shown to technical readers only. */
export function BianStrip({ bian, showOperation }) {
  if (!bian) return null;
  return (
    <div className={styles.bianStrip}>
      <span className={styles.bianLabel}>BIAN service domain</span>
      <span className={styles.bianDomains}>{bian.domains.join(" · ")}</span>
      {showOperation && bian.operation && (
        <code className={styles.bianOperation}>{bian.operation}</code>
      )}
    </div>
  );
}

/** A titled card; `tag` is a small qualifier beside the label. */
export function Card({ label, tag, children, note }) {
  return (
    <div className={styles.card}>
      {label && (
        <div className={styles.cardLabel}>
          {label}
          {tag && <span className={styles.cardTag}>{tag}</span>}
        </div>
      )}
      {children}
      {note && <div className={styles.cardNote}>{note}</div>}
    </div>
  );
}

const GATES = [
  ["Party authentication", ["customer_authenticated"]],
  ["Payment entitlement", ["account_active", "account_unrestricted", "customer_entitled", "payment_limit_available", "dual_approval"]],
];

const CHECK_TEXT = {
  customer_authenticated: "Customer authenticated",
  account_active: "Account active",
  account_unrestricted: "No debit restriction",
  customer_entitled: "User entitled to debit",
  payment_limit_available: "Payment limit available",
  dual_approval: "Required approval",
};

const MARK = {
  PASS: ["✓", "markPass"],
  WARN: ["!", "markWarn"],
  FAIL: ["✗", "markFail"],
};

function gateVerdict(rows) {
  const results = rows.map((c) => String(c.result || c.outcome || "").toUpperCase());
  if (!results.length) return ["not run", "verdictIdle"];
  if (results.includes("FAIL")) return ["failed", "verdictBad"];
  if (results.includes("WARN") || results.includes("PENDING")) return ["needs a look", "verdictWarn"];
  return ["passed", "verdictGood"];
}

/**
 * Stage 2's two gates as verdict cards, each listing its own checks in the order Doina's demo
 * display uses. The caller's facts (method, segment, rule) sit under the checks they explain.
 */
export function GateCards({ payment, checks }) {
  const a = payment?.authentication;
  const e = payment?.entitlement;
  const facts = {
    "Party authentication": [
      a?.method && `${a.method}${a.factorCount ? ` · ${a.factorCount} factor${a.factorCount > 1 ? "s" : ""}` : ""}`,
      a?.callerType && `caller: ${a.callerType}`,
      a?.sessionRef && `session ${a.sessionRef}`,
    ],
    "Payment entitlement": [
      e?.segment && `${e.segment} segment`,
      e?.signingRule && `signing rule: ${e.signingRule}`,
    ],
  };
  return (
    <div className={styles.cardGrid}>
      {GATES.map(([label, names]) => {
        const rows = (checks || []).filter((c) => names.includes(c.name));
        const [verdict, tone] = gateVerdict(rows);
        const lines = (facts[label] || []).filter(Boolean);
        return (
          <Card key={label} label={<>{label}<span className={`${styles.verdict} ${styles[tone]}`}>{verdict}</span></>}>
            {rows.length === 0 && <div className={styles.cardNote}>No checks recorded.</div>}
            {rows.map((c, i) => {
              const result = String(c.result || c.outcome || "").toUpperCase();
              const [glyph, cls] = MARK[result] || ["–", "markIdle"];
              return (
                <div className={styles.gateRow} key={`${c.name}-${i}`} title={c.detail || c.reason || undefined}>
                  <span className={`${styles.gateMark} ${styles[cls]}`} aria-label={result || "no result"}>{glyph}</span>
                  <span>{CHECK_TEXT[c.name] || c.name}</span>
                  {c.mode && <span className={styles.gateMode}>{c.mode}</span>}
                </div>
              );
            })}
            {lines.length > 0 && <div className={styles.gateFacts}>{lines.join(" · ")}</div>}
          </Card>
        );
      })}
    </div>
  );
}

/** The amount against the per-payment limit and the dual-approval threshold. */
export function LimitGauge({ payment }) {
  const e = payment?.entitlement;
  const amount = Number(payment?.amount);
  const limit = e?.perPaymentLimit != null ? Number(e.perPaymentLimit) : null;
  const dual = e?.dualApprovalThreshold != null ? Number(e.dualApprovalThreshold) : null;
  if (!Number.isFinite(amount) || (limit == null && dual == null)) return null;
  const top = Math.max(amount, limit || 0, dual || 0) * 1.1;
  const pct = (v) => `${Math.min(100, (v / top) * 100)}%`;
  const overLimit = limit != null && amount > limit;
  const needsDual = dual != null && amount > dual;
  const tone = overLimit ? styles.gaugeFillBad : needsDual ? styles.gaugeFillWarn : "";
  const ccy = payment?.currency;
  return (
    <Card label="Amount against the limits" tag={e?.segment}>
      <div className={styles.gauge} role="img" aria-label="Amount against limits">
        <span className={`${styles.gaugeFill} ${tone}`} style={{ width: pct(amount) }} />
        {dual != null && <span className={styles.gaugeTick} style={{ left: pct(dual) }} />}
        {limit != null && <span className={styles.gaugeTick} style={{ left: pct(limit) }} />}
      </div>
      <div className={styles.gaugeLegend}>
        <span>Amount {fmtAmount(amount, ccy)}</span>
        {dual != null && <span>| Dual approval above {fmtAmount(dual, ccy)}</span>}
        {limit != null && <span>| Per-payment limit {fmtAmount(limit, ccy)}</span>}
      </div>
      {needsDual && (
        <div className={styles.cardNote}>
          A second approver ({e?.dualApprovalBy || "—"}) is required{e?.dualApprovalSimulated ? ", simulated here" : ""}.
        </div>
      )}
    </Card>
  );
}

/** One check row: result mark, label, mode chip, with the backend's detail as a tooltip. */
function CheckLine({ result, label, mode, detail, value }) {
  const [glyph, cls] = MARK[result] || ["–", "markIdle"];
  return (
    <div className={styles.gateRow} title={detail || undefined}>
      <span className={`${styles.gateMark} ${styles[cls]}`} aria-label={result || "not applicable"}>{glyph}</span>
      <span>{label}{value ? `: ${value}` : ""}</span>
      {mode && <span className={styles.gateMode}>{mode}</span>}
    </div>
  );
}

const byName = (checks, ...names) =>
  (checks || []).find((c) => names.includes(c.name));
const resultOf = (c) => (c ? String(c.result || c.outcome || "").toUpperCase() : undefined);

const OUTCOME_ROUTE = {
  MATCHED: ["PASS", "Proceeds to stage 3"],
  PARTIAL: ["WARN", "Proceeds to stage 3, flagged"],
  NO_MATCH: ["FAIL", "Routes to stage 9, unable to apply"],
};

/** Inbound stage 2: the four checks Doina lists, in her order, from `checks[]` and the resolution. */
export function InboundResolutionChecks({ payment, checks }) {
  const r = payment?.beneficiaryResolution;
  const auth = byName(checks, "inbound_message_authenticated");
  const open = byName(checks, "beneficiary_account_open");
  // `beneficiary_resolved` is the name written before the account and name checks were split.
  const name = byName(checks, "beneficiary_name_matched", "beneficiary_resolved");
  const [routeResult, routeText] = OUTCOME_ROUTE[r?.matchOutcome] || [undefined, "Not decided yet"];
  return (
    <Card label="Checks performed" tag="sync">
      <CheckLine
        result={resultOf(auth)} mode={auth?.mode} detail={auth?.detail}
        label="Sending institution authenticated at the network level"
      />
      <CheckLine
        result={resultOf(open)} mode={open?.mode} detail={open?.detail}
        label="Claimed beneficiary account exists and is open"
        value={r?.accountStatus && r.accountStatus !== "ACTIVE" ? r.accountStatus : undefined}
      />
      <CheckLine
        result={resultOf(name)} mode={name?.mode} detail={name?.detail}
        label="Beneficiary name matches the account holder of record"
        value={r?.matchOutcome}
      />
      <CheckLine result={routeResult} label="Routed by outcome" value={routeText} />
    </Card>
  );
}

/** Inbound stage 3: sanctions of the originator, FX if required, domestic or cross-border. */
export function InboundValidationChecks({ payment, checks }) {
  const sanctions = byName(checks, "originator_screened");
  const fxDone = byName(checks, "inbound_fx_applied");
  const fxMissing = byName(checks, "inbound_fx_unavailable");
  const fx = fxDone || fxMissing;
  const corridor = byName(checks, "corridor_classified");
  const category = payment?.validation?.determinedCategory;
  const f = payment?.fx;
  return (
    <Card label="Checks performed" tag="sync">
      <CheckLine
        result={resultOf(sanctions)} mode={sanctions?.mode} detail={sanctions?.detail}
        label="Originator screened for sanctions and AML"
      />
      <CheckLine
        result={fx ? resultOf(fx) : "PASS"} mode={fx?.mode} detail={fx?.detail}
        label="Incoming FX conversion"
        value={fxDone && f?.fxRate
          ? `${f.sourceCurrency} to ${f.targetCurrency} at ${f.fxRate}`
          : fx ? undefined : "not required"}
      />
      <CheckLine
        result={resultOf(corridor)} mode={corridor?.mode} detail={corridor?.detail}
        label="Payment classified"
        value={category === "CROSS_BORDER" ? "Cross-border" : category === "DOMESTIC" ? "Domestic" : undefined}
      />
    </Card>
  );
}

const RESOLUTIONS = [
  ["MATCHED", "Account open, name matches", "Proceeds to stage 3", ""],
  ["PARTIAL", "Account open, name is a plausible variant", "Proceeds to stage 3, flagged", "tileOnWarn"],
  ["NO_MATCH", "No such account, closed, or an unrelated name", "Routes to stage 9, unable to apply", "tileOnBad"],
];

/** Inbound stage 2: the three beneficiary outcomes with the resolved one lit, and the claim beside the match. */
export function ResolutionOutcome({ payment }) {
  const r = payment?.beneficiaryResolution;
  if (!r) return null;
  // `creditor.name` is overwritten with the name of record on an exact match, so prefer the
  // claim persisted on the resolution; fall back for payments resolved before it existed.
  const claimedName = r.claimedName ?? payment?.creditor?.name;
  const claimedAccount = payment?.creditor?.accountNo ? `····${String(payment.creditor.accountNo).slice(-4)}` : null;
  return (
    <>
      <div className={styles.tiles} role="group" aria-label="Beneficiary match outcome">
        {RESOLUTIONS.map(([key, rule, next, tone]) => (
          <div key={key} className={`${styles.tile} ${key === r.matchOutcome ? `${styles.tileOn} ${styles[tone] || ""}` : ""}`}>
            <div className={styles.tileName}>{key === r.matchOutcome ? "● " : "○ "}{key}</div>
            <div className={styles.tileRule}>{rule}</div>
            <div className={styles.tileNext}>{next}</div>
          </div>
        ))}
      </div>
      <div className={styles.cardGrid}>
        <Card label="Claimed in the message" tag="from the sender">
          <div className={styles.gateRow}><span>{claimedName || "—"}</span></div>
          {claimedAccount && <div className={styles.gateFacts}>Account {claimedAccount}</div>}
        </Card>
        <Card label="Found at Leafy Bank" tag={r.matchMethod ? `match: ${r.matchMethod}` : undefined}>
          <div className={styles.gateRow}><span>{r.nameOfRecord || r.matchedAccountId || "no account matched"}</span></div>
          {r.nameOfRecord && r.matchedAccountId && <div className={styles.gateFacts}>Account {r.matchedAccountId}</div>}
          {r.checkedAt && <div className={styles.gateFacts}>Checked {fmtWhen(r.checkedAt)}</div>}
        </Card>
      </div>
    </>
  );
}

/** Outbound stage 4: Leafy Bank to correspondent to network to beneficiary bank, from the routing snapshot. */
export function RouteMap({ snapshot, payment }) {
  if (!snapshot) return null;
  const corr = snapshot.correspondent || {};
  const bene = payment?.creditor;
  return (
    <div className={styles.chain} role="group" aria-label="Route">
      <Node label="Leafy Bank" detail={snapshot.executionStrategy || "origin"} done />
      {corr.required && (
        <>
          <Hop label="via" />
          <Node
            label="Correspondent"
            detail={[corr.bankName || corr.bic, corr.simulated ? "simulated" : null].filter(Boolean).join(" · ") || (corr.resolved ? "resolved" : "unresolved")}
            done={!!corr.resolved}
          />
        </>
      )}
      <Hop label={snapshot.rail || "rail"} />
      <Node label={snapshot.clearingNetwork || "Network"} detail={snapshot.valueDate ? `value ${snapshot.valueDate}` : "network"} done />
      <Hop label="to" />
      <Node label="Beneficiary bank" detail={[bene?.bankName, bene?.bankCountry].filter(Boolean).join(" · ") || bene?.bic || "—"} done={!!bene} />
    </div>
  );
}

const FRAUD_REVIEW = 50;
const FRAUD_DECLINE = 80;

/** Fraud score against the review and decline thresholds, with the rules that fired. */
export function FraudMeter({ fraud, sanctions }) {
  if (fraud?.score == null) return null;
  const score = Number(fraud.score);
  const tone = score >= FRAUD_DECLINE ? styles.gaugeFillBad : score >= FRAUD_REVIEW ? styles.gaugeFillWarn : "";
  const rules = fraud.rulesFired || [];
  return (
    <Card label="Fraud evaluation" tag={fraud.decision}>
      <div className={styles.gauge} role="img" aria-label={`Fraud score ${score} of 100`}>
        <span className={`${styles.gaugeFill} ${tone}`} style={{ width: `${Math.min(100, score)}%` }} />
        <span className={styles.gaugeTick} style={{ left: `${FRAUD_REVIEW}%` }} />
        <span className={styles.gaugeTick} style={{ left: `${FRAUD_DECLINE}%` }} />
      </div>
      <div className={styles.gaugeLegend}>
        <span>Score {score}/100</span>
        <span>| Review at {FRAUD_REVIEW}</span>
        <span>| Decline at {FRAUD_DECLINE}</span>
      </div>
      <div className={styles.chipRow}>
        {rules.length ? rules.map((r) => <span key={r} className={styles.chip}>{r}</span>) : <span className={styles.chip}>no rules fired</span>}
        {sanctions?.status && (
          <span className={styles.chip}>sanctions {sanctions.status}{sanctions.provider ? ` · ${sanctions.provider}` : ""}</span>
        )}
      </div>
    </Card>
  );
}

/** Inbound stage 4: the beneficiary match and the sanctions result roll up into one decision. */
export function AcceptanceRollup({ payment }) {
  const ad = payment?.acceptanceDecision;
  if (!ad) return null;
  const accepted = ad.decision === "ACCEPT";
  return (
    <div className={styles.chain} role="group" aria-label="Acceptance roll-up">
      <Node label="Beneficiary match" detail={ad.beneficiaryMatch || "—"} done={!!ad.beneficiaryMatch} />
      <Hop label="+" />
      <Node label="Sanctions / AML" detail={ad.sanctionsStatus || "—"} done={!!ad.sanctionsStatus} />
      <Hop label="roll up" />
      <div className={`${styles.deltaChip} ${accepted ? styles.deltaMatch : styles.deltaShort}`}>
        {ad.decision || "—"}{ad.reasonCode ? ` · ${ad.reasonCode}` : ""}
      </div>
    </div>
  );
}

/** Stage 5: where the message is built. Outbound maps and submits; inbound answers; internal crosses no rail. */
export function RailFlow({ payment, data }) {
  const inbound = payment?.direction === "INBOUND";
  const e = data?.execution;
  if (inbound) {
    const sent = !!payment?.refs?.statusResponseMessageId;
    return (
      <div className={styles.chain} role="group" aria-label="Status response">
        <Node label="Acceptance" detail={payment?.acceptanceDecision?.decision || "—"} done={!!payment?.acceptanceDecision} />
        <Hop label="generate" />
        <Node label="pacs.002" detail={sent ? "ACCP" : "pending"} done={sent} />
        <Hop label="transmit" async />
        <Node label="Sending bank" detail={sent ? "told" : "waiting"} done={sent} />
      </div>
    );
  }
  if (!e) {
    return (
      <div className={styles.chain} role="group" aria-label="Book transfer">
        <Node label="Authorised payment" detail={payment?.paymentId || "—"} done={!!payment} />
        <Hop label="no rail leg" />
        <Node label="Ledger" detail="posts at stage 6" done />
      </div>
    );
  }
  return (
    <div className={styles.chain} role="group" aria-label="Rail execution">
      <Node label="Canonical payment" detail={payment?.paymentId || "—"} done />
      <Hop label={data?.mappingVersion ? `mapper ${data.mappingVersion}` : "ISO mapper"} />
      <Node label={e.messageFormat || "ISO 20022"} detail={e.messageStandard || "message"} done />
      <Hop label="submit" async />
      <Node label={e.clearingNetwork || payment?.rail || "Network"} detail={e.simulated ? "simulated" : "live"} done />
      <Hop label="status" async />
      <Node
        label="Rail status"
        detail={data?.railStatus?.code || e.status || "pending"}
        done={!!(data?.railStatus?.code || e.acknowledgedAt)}
      />
    </div>
  );
}

const SETTLEMENT_OUTCOMES = [
  ["MATCHED", "Exactly what was expected", "Straight to stage 8", ""],
  ["UNMATCHED", "Something came back, but it disagrees", "Stage 8 flags it; stage 9 investigates", "tileOnWarn"],
  ["DELAYED", "Nothing back inside the window", "Stays pending; may escalate", "tileOnWarn"],
  ["EXCEPTION", "Explicitly rejected or returned", "Straight to stage 9", "tileOnBad"],
];

/** Doina's four simulated settlement outcomes, the real one lit, each with where it leads. */
export function SettlementOutcomes({ position, direction }) {
  const outcome = position?.outcome;
  if (!outcome) return null;
  const inbound = direction === "INBOUND";
  return (
    <>
      <div className={styles.tiles} role="group" aria-label="Settlement outcome">
        {SETTLEMENT_OUTCOMES.map(([key, rule, next, tone]) => (
          <div key={key} className={`${styles.tile} ${key === outcome ? `${styles.tileOn} ${styles[tone] || ""}` : ""}`}>
            <div className={styles.tileName}>{key === outcome ? "● " : "○ "}{key}</div>
            <div className={styles.tileRule}>{rule}</div>
            <div className={styles.tileNext}>{next}</div>
          </div>
        ))}
      </div>
      <div className={styles.cardNote}>
        Second ledger event, separate from the stage 6 posting:{" "}
        {inbound ? "Dr Nostro/Central Bank Cash / Cr Wire clearing" : "Dr Wire clearing / Cr Nostro/Central Bank Cash"}.
      </div>
    </>
  );
}

/** The mirror posting an inbound or outbound wire makes at stage 6. */
export function PostingDirection({ payment, trace }) {
  if (payment?.rail !== "WIRE") return null;
  const inbound = payment?.direction === "INBOUND";
  // Show the posted accounts (code + name) once the ledger event exists; the generic role
  // names are the fallback before it does.
  const names = trace?.accountNames ?? {};
  const account = (leg, fallback) =>
    leg?.glAccountCode ? `${leg.glAccountCode} ${names[leg.glAccountCode] || fallback}` : fallback;
  const debit = account(trace?.ledgerEvent?.debitLeg, inbound ? "Wire clearing" : "Customer deposit liability");
  const credit = account(trace?.ledgerEvent?.creditLeg, inbound ? "Customer deposit liability" : "Wire clearing");
  return (
    <div className={styles.cardNote}>
      {inbound ? "Inbound posts the mirror of an outbound wire:" : "Outbound wire:"}{" "}
      Dr {debit} / Cr {credit}.
    </div>
  );
}
