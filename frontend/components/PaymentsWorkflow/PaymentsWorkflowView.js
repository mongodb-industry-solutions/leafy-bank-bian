"use client";

// Client shell for the Payments Workflow route (Payments Analyst persona).
//
// Two lenses, switched from the NavBar top bar (not a local toggle) via
// PaymentsWorkflowContext so the nav and this page share one source of truth:
//   * Payments  — the initiation wizard. On submit, the wizard's "View lifecycle" action
//     swaps in the PaymentDeepDive for the just-created payment *in this same lens*, so the
//     analyst watches the saga without hopping to Activity. Back returns to a fresh form.
//   * Activity  — the historical list; selecting a payment shows its lifecycle.
//
// Owns the per-lens selected payment and the single refresh key — the one-owner rule
// GlPipelineView establishes. No page-level poll: the list is a surface an analyst reads and
// filters, and refreshing it under a cursor is hostile. The only thing that changes while
// you watch it is the selected payment's ledger trace, and PaymentDeepDive already owns that
// poll (usePipelineTrace, self-terminating).
import { useCallback, useEffect, useState } from "react";

import styles from "./PaymentsWorkflow.module.css";
import InitiateWizard from "./InitiateWizard";
import PaymentsLens from "./PaymentsLens";
import PaymentDeepDive from "./PaymentDeepDive";
import { usePaymentsWorkflow, WORKFLOW_LENS } from "@/lib/context/PaymentsWorkflowContext";

export default function PaymentsWorkflowView() {
  const { lens } = usePaymentsWorkflow();
  const [refreshKey, setRefreshKey] = useState(0);
  // Activity lens: the selected historical payment; null means "show the list".
  const [selectedPaymentId, setSelectedPaymentId] = useState(null);
  // Payments lens: the payment just created via the wizard; null means "show the wizard".
  const [initiatePaymentId, setInitiatePaymentId] = useState(null);

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  // The wizard's "View lifecycle" action: show the deep dive for the new payment in place,
  // staying on the Payments lens — do not jump to Activity.
  const handleInitiated = useCallback(
    (paymentId) => {
      if (paymentId) setInitiatePaymentId(paymentId);
      refresh();
    },
    [refresh]
  );

  // Switching lens (from the NavBar) resets that lens's selection so neither re-shows a
  // stale lifecycle. Keyed on `lens`, not the trace, so re-polls don't clobber it.
  useEffect(() => {
    setSelectedPaymentId(null);
    setInitiatePaymentId(null);
  }, [lens]);

  return (
    <div className={styles.pwRoot}>
      <div id="pw-lens-panel" className={styles.lensPanel}>
        {lens === WORKFLOW_LENS.PAYMENTS &&
          (initiatePaymentId ? (
            <PaymentDeepDive
              paymentId={initiatePaymentId}
              refreshKey={refreshKey}
              onBack={() => setInitiatePaymentId(null)}
            />
          ) : (
            <InitiateWizard onInitiated={handleInitiated} />
          ))}

        {lens === WORKFLOW_LENS.ACTIVITY && (
          <PaymentsLens
            // Remounting on lens change resets filters and paging, which is what entering
            // Activity should do.
            key={lens}
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
