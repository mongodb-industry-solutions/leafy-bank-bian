"use client";

import { useUser } from "@/lib/context/UserContext";
import { BACKOFFICE_DECISION_KEY, seenOutcomesKey } from "@/lib/const/agentEngineBackofficeBridge";
import { getCustomer360ReviewOutcomes } from "@/lib/api/client";
import { Body } from "@leafygreen-ui/typography";
import { useEffect, useState } from "react";
import LeafyBankChatAssistant from "../LeafyBankChatAssistant/LeafyBankChatAssistant";
import styles from "./FloatingAssistant.module.css";

const BRIDGE_APPLIED_KEY = "agentEngineBridgeAppliedOutcomes";

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
  // back-office approve/deny) — see lib/const/agentEngineBackofficeBridge.js.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(BACKOFFICE_DECISION_KEY);
      if (!raw) return;
      const decision = JSON.parse(raw);
      setPendingResolution(decision);
      // Remember it so the outcome poll below doesn't show the same one twice.
      if (decision?.executionId) {
        const ids = JSON.parse(localStorage.getItem(BRIDGE_APPLIED_KEY) || "[]");
        localStorage.setItem(BRIDGE_APPLIED_KEY, JSON.stringify([...ids, decision.executionId].slice(-50)));
      }
      setModalOpen(true);
      setHasUnread(false);
    } catch {
      localStorage.removeItem(BACKOFFICE_DECISION_KEY);
    }
  }, []);

  // A back-office decision usually lands after the customer has left the page
  // (or in another browser), so the localStorage hand-off above can't be the
  // only way they find out. Poll the engine for this customer's finished
  // reviews and put the assistant's own message about each into the chat,
  // with the unread badge. Outcomes that already existed on first load are
  // marked seen rather than replayed.
  const backendUserId = selectedUser?.bankUsername;
  useEffect(() => {
    if (!backendUserId) return undefined; // back-office personas have no customer chat
    const key = seenOutcomesKey(backendUserId);
    let cancelled = false;

    async function poll() {
      const { data, error } = await getCustomer360ReviewOutcomes(backendUserId);
      if (cancelled || error || !data?.outcomes) return;
      let seen;
      let firstRun = false;
      try {
        const raw = localStorage.getItem(key);
        firstRun = raw === null;
        seen = new Set(raw ? JSON.parse(raw) : []);
        JSON.parse(localStorage.getItem(BRIDGE_APPLIED_KEY) || "[]").forEach((id) => seen.add(id));
      } catch {
        seen = new Set();
      }
      const fresh = data.outcomes.filter((o) => !seen.has(o.execution_id));
      if (fresh.length === 0) return;
      fresh.forEach((o) => seen.add(o.execution_id));
      try {
        localStorage.setItem(key, JSON.stringify([...seen]));
      } catch {
        /* ignore — worst case an outcome is shown again after a reload */
      }
      if (firstRun) return; // baseline: history, not news
      // Oldest first, so several outcomes land in the order they happened.
      [...fresh].reverse().forEach((o) => {
        setPendingResolution({ executionId: o.execution_id, decision: o.decision, response: o.result });
      });
      setHasUnread(true);
    }

    poll();
    const interval = setInterval(poll, 8000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [backendUserId]);

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
