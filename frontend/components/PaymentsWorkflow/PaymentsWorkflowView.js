"use client";

// Client shell for the Payments Workflow route (Payments Analyst persona).
//
// One surface: the payment history list, with its lifecycle deep-dive on selection. A
// persistent "New payment" button (bottom centre, like Leafy Bank's Send Money) opens the
// initiation wizard in a modal over the list. When the wizard's "View lifecycle" action
// fires, the modal closes and the new payment is selected in the list.
//
// Owns the selected payment and the single refresh key — the one-owner rule GlPipelineView
// establishes. No page-level poll: the list is a surface an analyst reads and filters, and
// refreshing it under a cursor is hostile. The only thing that changes while you watch it is
// the selected payment's ledger trace, and PaymentDeepDive already owns that poll.
import { useCallback, useState } from "react";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";

import styles from "./PaymentsWorkflow.module.css";
import InitiateWizard from "./InitiateWizard";
import PaymentsLens from "./PaymentsLens";

export default function PaymentsWorkflowView() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedPaymentId, setSelectedPaymentId] = useState(null);
  const [wizardOpen, setWizardOpen] = useState(false);

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const handleInitiated = useCallback(
    (paymentId) => {
      setWizardOpen(false);
      if (paymentId) setSelectedPaymentId(paymentId);
      refresh();
    },
    [refresh]
  );

  return (
    <div className={styles.pwRoot}>
      <div id="pw-lens-panel" className={styles.lensPanel}>
        <PaymentsLens
          refreshKey={refreshKey}
          onRefresh={refresh}
          selectedPaymentId={selectedPaymentId}
          onSelect={setSelectedPaymentId}
        />
      </div>

      <div className={styles.newPaymentBar}>
        <Button variant="baseGreen" leftGlyph={<Icon glyph="Plus" />} onClick={() => setWizardOpen(true)}>
          New payment
        </Button>
      </div>

      {wizardOpen && (
        <div
          className={styles.wizardOverlay}
          role="dialog"
          aria-modal="true"
          aria-label="New payment"
          onMouseDown={(e) => e.target === e.currentTarget && setWizardOpen(false)}
        >
          <div className={styles.wizardDialog}>
            <button
              type="button"
              className={styles.wizardClose}
              aria-label="Close"
              onClick={() => setWizardOpen(false)}
            >
              <Icon glyph="X" />
            </button>
            <InitiateWizard onInitiated={handleInitiated} />
          </div>
        </div>
      )}
    </div>
  );
}
