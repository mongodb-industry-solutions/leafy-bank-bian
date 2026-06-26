import React from "react";
import { H2, Body } from "@leafygreen-ui/typography";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import styles from "./AccountModal.module.css";

export default function AccountModal({
  isOpen,
  onClose,
  accountBalance,
  setAccountBalance,
  accountType,
  setAccountType,
}) {
  if (!isOpen) return null;

  return (
    <div className={styles.modalBackdrop}>
      <div className={styles.modalDialog} role="dialog" aria-modal="true">
        <div className={styles.modalHeader}>
          <H2>Open new account</H2>
          <button
            className={styles.modalCloseButton}
            onClick={onClose}
            aria-label="Close open new account modal"
          >
            <Icon glyph="X" />
          </button>
        </div>
        <div className={styles.modalBody}>
          <div className={styles.formGroup}>
            <label className={styles.formLabel} htmlFor="account-number">
              Account number
            </label>
            <input
              id="account-number"
              className={styles.formInput}
              type="text"
              placeholder="Auto-generated"
              disabled
            />
          </div>
          <div className={styles.formGroup}>
            <label className={styles.formLabel} htmlFor="account-balance">
              Account balance
            </label>
            <input
              id="account-balance"
              className={styles.formInput}
              type="number"
              value={accountBalance}
              onChange={(e) => setAccountBalance(e.target.value)}
            />
          </div>
          <div className={styles.formGroup}>
            <label className={styles.formLabel} htmlFor="account-type">
              Account type
            </label>
            <input
              id="account-type"
              className={styles.formInput}
              type="text"
              value={accountType}
              onChange={(e) => setAccountType(e.target.value)}
            />
          </div>
        </div>
        <div className={styles.modalActions}>
          <Button variant="primary" onClick={onClose}>
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}
