"use client";

import React from "react";
import styles from "./BottomNav.module.css";

/**
 * Mobile-only bottom navigation bar (iOS app style, à la Airbnb).
 * Hidden on desktop via CSS — only shown at <= 768px.
 *
 * Props:
 *   items: Array<{ key, label, icon, onClick }>
 *     - icon: a ReactNode rendered above the label. Swap these for the
 *       final icons when they're provided.
 */
const BottomNav = ({ items = [] }) => {
  return (
    <nav className={styles.bottomNav} aria-label="Quick actions">
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          className={styles.navItem}
          onClick={item.onClick}
          aria-label={item.label}
        >
          {item.icon && <span className={styles.navIcon}>{item.icon}</span>}
          <span className={styles.navLabel}>{item.label}</span>
        </button>
      ))}
    </nav>
  );
};

export default BottomNav;
