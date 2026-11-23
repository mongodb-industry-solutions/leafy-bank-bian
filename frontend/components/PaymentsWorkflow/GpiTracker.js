"use client";

// Simulated SWIFT gpi tracking for one payment, from `payment.gpiEvents[]` + `payment.tracking`
// (backend: contexts/payment_rail/domain/gpi.py). Renders nothing for untracked payments.
import { useState } from "react";
import IconButton from "@leafygreen-ui/icon-button";
import Icon from "@leafygreen-ui/icon";
import Tooltip from "@leafygreen-ui/tooltip";

import styles from "./PaymentsWorkflow.module.css";
import StatusPill from "./StatusPill";
import { fmtWhen } from "@/lib/paymentsWorkflow/status";
import {
  GPI_REASON,
  HOP_ROLE,
  TRACKING,
  elapsed,
  gpiStatusLabel,
  labelOf,
} from "./gpiLabels";

export function TrackingPill({ tracking }) {
  const entry = TRACKING[tracking?.trackingStatus];
  if (!entry) return null;
  return <StatusPill family={entry.family} label={`gpi · ${entry.label}`} />;
}

function CopyUetr({ uetr }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    // Clipboard API is unavailable on non-HTTPS origins; fail quietly.
    try {
      await navigator.clipboard.writeText(uetr);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* no-op */
    }
  };

  return (
    <Tooltip
      trigger={
        <IconButton aria-label="Copy UETR" onClick={copy}>
          <Icon glyph="Copy" />
        </IconButton>
      }
    >
      {copied ? "Copied" : `Copy UETR ${uetr}`}
    </Tooltip>
  );
}

// Fallback when the payment predates `tracking`: derive what the narrative needs from the last event.
function trackingFrom(events) {
  const last = events[events.length - 1];
  return {
    trackingStatus: last.outcome ?? (last.hop?.role === "DEBTOR_AGENT" ? "SENT" : "IN_TRANSIT"),
    reasonCode: last.reasonCode,
    lastBic: last.hop?.bic,
    lastUpdatedAt: last.at,
    hopsConfirmed: new Set(events.filter((e) => e.gpiStatus !== "RJCT").map((e) => e.hop?.seq)).size,
    hopsTotal: new Set(events.map((e) => e.hop?.seq)).size,
  };
}

function narrative(tracking, events, direction) {
  const { trackingStatus, lastBic, hopsConfirmed, hopsTotal, reasonCode, lastUpdatedAt } = tracking;
  const took = elapsed(events[0].at, lastUpdatedAt ?? events[events.length - 1].at);
  const reason = labelOf(GPI_REASON, reasonCode);

  if (direction === "INBOUND") {
    return trackingStatus === "REJECTED" || trackingStatus === "RETURNED"
      ? `Returned to the sender by Leafy Bank: ${reason}.`
      : "Received and credited. Leafy Bank reported the final status.";
  }
  switch (trackingStatus) {
    case "CREDITED":
      return `Credited at ${lastBic} after ${hopsTotal} banks in ${took}.`;
    case "REJECTED":
      return `Rejected by ${lastBic}: ${reason}.`;
    case "RETURNED":
      return `Credited, then returned by ${lastBic}: ${reason}.`;
    default:
      return `With ${lastBic}: ${hopsConfirmed} of ${hopsTotal} banks confirmed, ${took} so far.`;
  }
}

function HopTable({ events }) {
  return (
    <table className={styles.gpiTable}>
      <thead>
        <tr>
          <th>#</th>
          <th>At</th>
          <th>Role</th>
          <th>BIC</th>
          <th>Message</th>
          <th>gpi status</th>
          <th>Reason</th>
          <th>Reportable</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => {
          const rowClass = [
            e.outcome ? styles.gpiRowFinal : "",
            e.reportable === false ? styles.gpiRowUnreported : "",
          ].join(" ").trim();
          return (
            <tr key={e.seq} className={rowClass || undefined}>
              <td className={styles.mono}>{e.seq}</td>
              <td>{fmtWhen(e.at)}</td>
              <td>{labelOf(HOP_ROLE, e.hop?.role)}</td>
              <td className={styles.mono}>
                {e.hop?.bic ?? "—"}
                {e.hop?.country ? ` (${e.hop.country})` : ""}
              </td>
              <td className={styles.mono}>
                {e.fromBic ?? "—"} → {e.toBic ?? "—"}
              </td>
              <td>
                <span className={styles.mono}>{e.gpiStatus}</span> {gpiStatusLabel(e)}
              </td>
              <td>
                {e.reasonCode ? (
                  <>
                    <span className={styles.mono}>{e.reasonCode}</span>{" "}
                    {labelOf(GPI_REASON, e.reasonCode)}
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td>{e.reportable === false ? <span className={styles.muted}>internal only</span> : "✓"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// One chip per report, in seq order: the bank and what it did. Hover shows role and time.
function HopChain({ events }) {
  return (
    <ol className={styles.gpiChain}>
      {events.map((e, i) => {
        const tone =
          e.gpiStatus === "RJCT" ? styles.gpiHopBad : e.gpiStatus === "ACCC" ? styles.gpiHopDone : "";
        return (
          <li key={e.seq ?? i} className={styles.gpiChainItem}>
            {i > 0 && <span className={styles.gpiArrow} aria-hidden>→</span>}
            <span
              className={`${styles.gpiHop} ${tone}`}
              title={`${labelOf(HOP_ROLE, e.hop?.role)} · ${fmtWhen(e.at)}`}
            >
              <span className={styles.mono}>{e.hop?.bic ?? "—"}</span>
              <span className={styles.gpiHopStatus}>{gpiStatusLabel(e)}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export default function GpiTracker({ payment, lens }) {
  const [showHops, setShowHops] = useState(false);
  if (!payment?.gpiEvents?.length) return null;

  const events = [...payment.gpiEvents].sort((a, b) => a.seq - b.seq);
  const tracking = payment.tracking ?? trackingFrom(events);
  const showTechnical = lens === "technical" || lens === "both";

  return (
    <section className={styles.gpiTracker}>
      <div className={styles.gpiHead}>
        <span className={styles.gpiTitle}>SWIFT gpi</span>
        <TrackingPill tracking={tracking} />
        <StatusPill family="gray" label="Simulated" />
        <span className={styles.gpiNarrative}>{narrative(tracking, events, payment.direction)}</span>
        {events[0].uetr && <CopyUetr uetr={events[0].uetr} />}
      </div>

      <HopChain events={events} />

      {showTechnical && (
        <>
          <button
            type="button"
            className={styles.disclosure}
            onClick={() => setShowHops((v) => !v)}
            aria-expanded={showHops}
          >
            <Icon glyph={showHops ? "ChevronDown" : "ChevronRight"} size={12} />
            {showHops ? "Hide gpi reports" : "Show gpi reports"}
          </button>
          {showHops && <HopTable events={events} />}
        </>
      )}
    </section>
  );
}
