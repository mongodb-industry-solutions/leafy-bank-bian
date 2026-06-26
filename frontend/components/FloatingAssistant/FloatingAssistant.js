"use client";

import { useUser } from "@/lib/context/UserContext";
import { Body } from "@leafygreen-ui/typography";
import { useEffect, useState } from "react";
import LeafyBankAssistant from "../LeafyBankAssistant/LeafyBankAssistant";
import styles from "./FloatingAssistant.module.css";

export default function FloatingAssistant() {
  const { selectedUser } = useUser();
  const [modalOpen, setModalOpen] = useState(false);

  const [showBubble, setShowBubble] = useState(true);
  const [fadeOut, setFadeOut] = useState(false);

  const toggleChatbot = () => {
    setModalOpen(true);
  };

  useEffect(() => {
    const timer = setTimeout(() => {
      setFadeOut(true);
      setTimeout(() => setShowBubble(false), 500);
    }, 4000);

    return () => clearTimeout(timer);
  }, []);

  if (!selectedUser) return null;

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

        <img src="/agent.png" alt="Chat Icon" className={styles.chatIcon} />

        <div className={styles.textWrapper}>
          <Body className={styles.chatbotText}>Leafy Assistant</Body>

          <div className={styles.statusWrapper}>
            <div className={styles.indicator}></div>
            <Body>Available</Body>
          </div>
        </div>
      </div>

      <LeafyBankAssistant
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        initialPrompt="What can you tell me about my Leafy Bank data?"
      />
    </>
  );
}
