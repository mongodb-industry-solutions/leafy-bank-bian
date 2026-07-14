"use client";

import React, { useState, useEffect } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import Icon from "@leafygreen-ui/icon";
import Modal from "@leafygreen-ui/modal";
import { Body } from "@leafygreen-ui/typography";
import styles from "./GlMonitor.module.css";
import { useUser } from "@/lib/context/UserContext";

// Full-screen back-office header. This route is "bare" (no global NavBar — see
// AppShell), so user identity, the details modal, and switching are wired here
// directly off UserContext, mirroring NavBar.
export default function TopBar({ live }) {
  const [mounted, setMounted] = useState(false);
  const [showUserModal, setShowUserModal] = useState(false);
  const { selectedUser, clearUser } = useUser();
  const router = useRouter();

  // UserContext hydrates from localStorage on mount; guard SSR to avoid a
  // hydration mismatch on the user block.
  useEffect(() => setMounted(true), []);

  const userName = (mounted && selectedUser?.name) || "User";
  const userRole = (mounted && selectedUser?.role) || "";
  const userId = mounted && selectedUser?.id;
  const avatarSrc = userId ? `/users/${userId}.png` : "/user_avatar.png";

  const handleSwitchUser = (e) => {
    e?.stopPropagation();
    clearUser();
    router.push("/");
  };

  return (
    <>
      <Modal open={showUserModal} setOpen={setShowUserModal} aria-label="User info">
        <div className={styles["user-modal-content"]}>
          <Image src={avatarSrc} alt={userName} width={150} height={80} className={styles["user-image-modal"]} />
          <Body weight="medium">{userName}</Body>
          {userRole && <Body className={styles["user-role"]}>{userRole}</Body>}
          <div className={styles["user-tags"]}>
            <div className={styles["user-tag"]}>Employer: {selectedUser?.employer || "N/A"}</div>
            <div className={styles["user-tag"]}>Type: {selectedUser?.employmentType || "N/A"}</div>
            <div className={styles["user-tag"]}>Job Title: {selectedUser?.jobTitle || "N/A"}</div>
            <div className={styles["user-tag"]}>Spending Profile: {selectedUser?.spendingProfile || "N/A"}</div>
          </div>
          <button className={styles["switch-user-modal-btn"]} onClick={handleSwitchUser}>
            <Icon glyph="Refresh" size="small" /> Switch user
          </button>
        </div>
      </Modal>

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
          <div className={styles["user-info-container"]} onClick={() => setShowUserModal(true)}>
            <Image src={avatarSrc} alt={userName} className={styles["user-avatar"]} width={40} height={40} />
            <div className={styles["user-details"]}>
              <span className={styles["user-name"]}>{userName}</span>
              {userRole && <span className={styles["user-role"]}>{userRole}</span>}
            </div>
            <button
              className={styles["switch-user-btn"]}
              onClick={handleSwitchUser}
              aria-label="Switch user"
              title="Switch user"
            >
              <Icon glyph="Refresh" size="small" />
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
