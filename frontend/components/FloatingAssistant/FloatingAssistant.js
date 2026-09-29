"use client";

import { useUser } from "@/lib/context/UserContext";
import { BACKOFFICE_DECISION_KEY } from "@/lib/const/magentaBackofficeBridge";
import { Body } from "@leafygreen-ui/typography";
import { useEffect, useState } from "react";
import LeafyBankChatAssistant from "../LeafyBankChatAssistant/LeafyBankChatAssistant";
import styles from "./FloatingAssistant.module.css";

export default function FloatingAssistant() {
  // selectedUser is set as soon as a login flow starts (to prefetch dashboard data in
  // the background) — loginInProgress excludes that window so this bubble doesn't
  // appear while the login modal/overlay is still on screen.
  const { selectedUser, loginInProgress } = useUser();
  const [modalOpen, setModalOpen] = useState(false);
  const [pendingResolution, setPendingResolution] = useState(null);

  const [showBubble, setShowBubble] = useState(true);
  const [fadeOut, setFadeOut] = useState(false);
  // The agent proactively has something to say before the customer ever opens
  // the chat — mirrors the "!" unread indicator on a real messaging app, and
  // clears once they open it and start reading.
  const [hasUnread, setHasUnread] = useState(true);

  const toggleChatbot = () => {
    setModalOpen(true);
    setHasUnread(false);
  };

  useEffect(() => {
    const timer = setTimeout(() => {
      setFadeOut(true);
      setTimeout(() => setShowBubble(false), 500);
    }, 4000);

    return () => clearTimeout(timer);
  }, []);

  // Picked up once, right when this mounts (e.g. landing back on "/" after a
  // back-office approve/deny) — see lib/const/magentaBackofficeBridge.js.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(BACKOFFICE_DECISION_KEY);
      if (!raw) return;
      setPendingResolution(JSON.parse(raw));
      setModalOpen(true);
      setHasUnread(false);
    } catch {
      localStorage.removeItem(BACKOFFICE_DECISION_KEY);
    }
  }, []);

  function handleResolutionConsumed() {
    localStorage.removeItem(BACKOFFICE_DECISION_KEY);
    setPendingResolution(null);
  }

  if (!selectedUser || loginInProgress) return null;

  return (
    <>
      <div
        className={styles.chatbotButton}
        onClick={toggleChatbot}
      >
        {showBubble && (
          <div
            className={`${styles.speechBubble} ${
              fadeOut ? styles.fadeOut : styles.fadeIn
            }`}
          >
            Can I help you?
          </div>
        )}

        {hasUnread && <span className={styles.unreadBadge}>!</span>}

        <img src="/agent.png" alt="Chat Icon" className={styles.chatIcon} />

        <div className={styles.textWrapper}>
          <Body className={styles.chatbotText}>Leafy Assistant</Body>

          <div className={styles.statusWrapper}>
            <div className={styles.indicator}></div>
            <Body>Available</Body>
          </div>
        </div>
      </div>

      <LeafyBankChatAssistant
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        pendingResolution={pendingResolution}
        onResolutionConsumed={handleResolutionConsumed}
      />
    </>
  );
}
