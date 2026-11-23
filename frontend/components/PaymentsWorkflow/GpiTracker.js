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

export default function GpiTracker({ payment, lens }) {
  if (!payment?.gpiEvents?.length) return null;

  const events = [...payment.gpiEvents].sort((a, b) => a.seq - b.seq);
  const tracking = payment.tracking ?? trackingFrom(events);
  const showBusiness = lens === "business" || lens === "both";
  const showTechnical = lens === "technical" || lens === "both";
  const total = tracking.hopsTotal || 0;
  // A final outcome ends the journey: inbound only ever records our own hop, and a rejection
  // stops before the remaining hops, so hop counts alone would leave the bar part-full.
  const isFinal = ["CREDITED", "REJECTED", "RETURNED"].includes(tracking.trackingStatus);
  const confirmed = isFinal ? total : tracking.hopsConfirmed || 0;
  const pct = total ? Math.min(100, Math.round((confirmed / total) * 100)) : 0;

  return (
    <section className={styles.gpiTracker}>
      <div className={styles.gpiHead}>
        <strong>SWIFT gpi tracking</strong>
        <TrackingPill tracking={tracking} />
        <StatusPill family="gray" label="Simulated" />
        {events[0].uetr && <CopyUetr uetr={events[0].uetr} />}
      </div>

      {showBusiness && (
        <>
          <div
            className={styles.gpiProgress}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={confirmed}
          >
            <span style={{ width: `${pct}%` }} />
          </div>
          <p className={styles.gpiNarrative}>{narrative(tracking, events, payment.direction)}</p>
        </>
      )}

      {showTechnical && <HopTable events={events} />}
    </section>
  );
}
