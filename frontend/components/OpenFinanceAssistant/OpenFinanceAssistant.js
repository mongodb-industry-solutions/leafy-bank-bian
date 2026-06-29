"use client";

import { useOpenFinanceChatbot } from "@/lib/api/useOpenFinanceChatbot";
import { TALK_TRACK } from "@/lib/const/talkTrack";
import Button from "@leafygreen-ui/button";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import { Body, H2, H3 } from "@leafygreen-ui/typography";
import { useEffect, useRef, useState } from "react";

import styles from "./OpenFinanceAssistant.module.css";

export default function OpenFinanceAssistant({ isOpen, onClose }) {
  const {
    messages,
    inputValue,
    setInputValue,
    sending,
    suggestions,
    handleSend,
    handleKeyDown,
    renderMarkdown,
  } = useOpenFinanceChatbot();

  const [activeTab, setActiveTab] = useState(0);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  useEffect(() => {
    if (isOpen) {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className={styles.customModalBackdrop} onClick={onClose}>
      <div className={styles.customModalContainer} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeButton} onClick={onClose}>×</button>

        <div className={styles.chatContainer}>
          <div className={styles.chatHeader}>
            <div className={styles.chatHeaderContent}>
              <img src="/agent.png" alt="Agent" className={styles.agentImage} />
              <div className={styles.headerTitleWrapper}>
                <H2 className={styles.chatTitle}>Open Finance Advisor</H2>
                <div className={styles.status}>
                  <span className={styles.pulseDot} />
                  Available
                </div>
              </div>
            </div>
          </div>

          <div className={styles.tabsWrapper}>
            <Tabs
              aria-label="Open Finance chat tabs"
              selected={activeTab}
              setSelected={setActiveTab}
            >
              <Tab name="AI Assistant">
                <div className={styles.chatTabContent}>
                  <div className={styles.chatMessages}>
                    {messages.map((msg, i) => (
                      <div
                        key={i}
                        className={`${styles.message} ${
                          msg.type === "user"
                            ? styles.userMessage
                            : msg.type === "error"
                              ? styles.errorMessage
                              : msg.type === "interrupt"
                                ? styles.interruptMessage
                                : styles.assistantMessage
                        }`}
                      >
                        {msg.type === "assistant" ? (
                          <div className={styles.assistantContent}>
                            <div
                              className={styles.messageText}
                              dangerouslySetInnerHTML={renderMarkdown(msg.text)}
                            />
                          </div>
                        ) : (
                          <Body className={styles.messageText}>{msg.text}</Body>
                        )}
                      </div>
                    ))}

                    {suggestions.length > 0 && (
                      <div className={styles.suggestions}>
                        {suggestions.map((text, i) => (
                          <button
                            key={i}
                            className={styles.suggestionChip}
                            onClick={() => handleSend(text)}
                          >
                            {text}
                          </button>
                        ))}
                      </div>
                    )}

                    {sending && (
                      <div className={styles.stepIndicator}>
                        <div className={styles.stepHeader}>
                          <div className={styles.spinner} />
                          <span>Thinking...</span>
                        </div>
                      </div>
                    )}

                    <div ref={messagesEndRef} />
                  </div>

                  <div className={styles.chatInputContainer}>
                    <input
                      ref={inputRef}
                      type="text"
                      placeholder={sending ? "Thinking..." : "Ask about your finances..."}
                      value={inputValue}
                      onChange={(e) => setInputValue(e.target.value)}
                      onKeyDown={handleKeyDown}
                      className={styles.chatInput}
                      disabled={sending}
                    />
                    <Button
                      variant="primary"
                      onClick={() => handleSend()}
                      disabled={sending || !inputValue.trim()}
                    >
                      Send
                    </Button>
                  </div>
                </div>
              </Tab>

              {TALK_TRACK.map((tab, idx) => (
                <Tab key={idx} name={tab.heading}>
                  <div className={styles.scrollableTabContent}>
                    {tab.content.map((item, i) => (
                      <div key={i} className={styles.tabSection}>
                        {item.heading && (
                          <H3 className={styles.tabSectionHeading}>
                            {item.heading}
                          </H3>
                        )}
                        {item.body && (
                          <div
                            className={styles.markdownBody}
                            dangerouslySetInnerHTML={renderMarkdown(item.body)}
                          />
                        )}
                        {item.image && (
                          <img
                            src={item.image.src}
                            alt={item.image.alt}
                            className={styles.tabImage}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                </Tab>
              ))}
            </Tabs>
          </div>
        </div>
      </div>
    </div>
  );
}
