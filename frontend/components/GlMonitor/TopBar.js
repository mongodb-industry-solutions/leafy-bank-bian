import React from "react";
import Image from "next/image";
import styles from "./GlMonitor.module.css";

// The base-URL field is kept for visual parity with the static reference, but
// data now flows through the Next.js /api/backend proxy, so it is display-only.
export default function TopBar({ live }) {
  return (
    <div className={styles.topbar}>
      <div className={styles["topbar-left"]}>
        <div className={styles["topbar-logo"]}>
          <Image src="/leafy_bank_logo.png" alt="Leafy Bank" width={140} height={50} style={{ height: 50, width: "auto" }} />
        </div>
      </div>

      <div className={styles["topbar-right"]}>
        <div
          className={styles["live-dot"]}
          title="Live"
          style={{
            background: live ? "#4ade80" : "#f87171",
            boxShadow: live ? "0 0 0 2px #166534" : "0 0 0 2px #7f1d1d",
          }}
        />
        <div className={styles["user-info-container"]}>
          <Image src="/user_avatar.png" alt="User avatar" className={styles["user-avatar"]} width={40} height={40} />
          <div className={styles["user-details"]}>
            <span className={styles["user-name"]}>marcowenz</span>
            <span className={styles["user-role"]}>Bank Ops Admin</span>
          </div>
        </div>
      </div>
    </div>
  );
}
