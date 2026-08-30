"use client";

// Client shell for the Payments Workflow route (Payments Analyst persona).
//
// Owns the lens selection, the selected payment, and the single refresh key — the
// one-owner rule GlPipelineView establishes. Deliberately no page-level poll: the list is
// a surface an analyst reads and filters, and refreshing it under a cursor is hostile. The
// only thing that changes while you watch it is the selected payment's ledger trace, and
// PaymentTrace already owns that poll (usePipelineTrace, self-terminating).
import { useCallback, useState } from "react";
import { SegmentedControl, SegmentedControlOption } from "@leafygreen-ui/segmented-control";
import { H2, Body } from "@leafygreen-ui/typography";

import styles from "./PaymentsWorkflow.module.css";
import InitiateWizard from "./InitiateWizard";
import PaymentsLens from "./PaymentsLens";

const LENS = { INITIATE: "initiate", PAYMENTS: "payments", OPERATIONS: "operations" };

export default function PaymentsWorkflowView() {
  const [lens, setLens] = useState(LENS.PAYMENTS);
  const [refreshKey, setRefreshKey] = useState(0);
  // Selecting a payment drills into its lifecycle; null means "show the list".
  const [selectedPaymentId, setSelectedPaymentId] = useState(null);

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  // After initiating, jump straight to the trace for the payment just created — the demo's
  // whole point is watching one payment move through the lifecycle.
  const handleInitiated = useCallback(
    (paymentId) => {
      if (paymentId) setSelectedPaymentId(paymentId);
      refresh();
      setLens(LENS.PAYMENTS);
    },
    [refresh]
  );

  return (
    <div className={styles.pwRoot}>
      <header className={styles.header}>
        <H2>Payments Workflow</H2>
        <Body className={styles.subtitle}>
          Bank-assisted initiation and end-to-end traceability across the payment lifecycle.
        </Body>
        <div className={styles.tabs}>
          <SegmentedControl
            name="pw-lens"
            value={lens}
            // Switching lens returns to that lens's list. Without this, opening a payment
            // in Payments and switching to Operations would show that payment's lifecycle
            // instead of the exception list. `handleInitiated` sets the lens directly, so
            // its deliberate select-then-switch is unaffected.
            onChange={(v) => {
              setLens(v);
              setSelectedPaymentId(null);
            }}
            aria-label="Payments workflow lens"
            // LG requires aria-controls on the parent or every option; the lenses all
            // render into the same panel below.
            aria-controls="pw-lens-panel"
          >
            <SegmentedControlOption value={LENS.INITIATE}>Initiate</SegmentedControlOption>
            <SegmentedControlOption value={LENS.PAYMENTS}>Payments</SegmentedControlOption>
            <SegmentedControlOption value={LENS.OPERATIONS}>Operations</SegmentedControlOption>
          </SegmentedControl>
        </div>
      </header>

      <div id="pw-lens-panel" className={styles.lensPanel}>
        {lens === LENS.INITIATE && <InitiateWizard onInitiated={handleInitiated} />}

        {lens !== LENS.INITIATE && (
          <PaymentsLens
            // Remounting on lens change resets filters and paging, which is what switching
            // between "all payments" and "exceptions only" should do.
            key={lens}
            exceptionsOnly={lens === LENS.OPERATIONS}
            refreshKey={refreshKey}
            onRefresh={refresh}
            selectedPaymentId={selectedPaymentId}
            onSelect={setSelectedPaymentId}
          />
        )}
      </div>
    </div>
  );
}
