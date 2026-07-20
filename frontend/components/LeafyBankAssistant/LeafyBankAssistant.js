"use client";

import { useChatbot } from "@/lib/api/useChatbot";
import { TALK_TRACK } from "@/lib/const/talkTrack";
import Button from "@leafygreen-ui/button";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import { Body, H2, H3 } from "@leafygreen-ui/typography";
import { useEffect, useRef, useState } from "react";

import styles from "./LeafyBankAssistant.module.css";

const SUGGESTIONS = [
  "Can I overdraft my account for payments and transfers?",
  "Am I going to be notified when overdraft interests will be charged?",
];

function SupportingDocs({ docs }) {
  const [enlarged, setEnlarged] = useState(null);
  if (!docs || docs.length === 0) return null;

  return (
    <div className={styles.supportingDocs}>
      <span className={styles.supportingDocsLabel}>Source pages ({docs.length})</span>
      <div className={styles.supportingDocImages}>
        {docs.map((doc, i) =>
          doc.image ? (
            <img
              key={i}
              src={`data:image/png;base64,${doc.image}`}
              alt={`Source page ${i + 1}`}
              className={`${styles.supportingDocImage} ${enlarged === i ? styles.supportingDocImageEnlarged : ""}`}
              onClick={() => setEnlarged(enlarged === i ? null : i)}
            />
          ) : null
        )}
      </div>
    </div>
  );
}

export default function LeafyBankAssistant({ isOpen, onClose, initialPrompt }) {
  const {
    messages,
    inputValue,
    setInputValue,
    sending,
    handleSend,
    handleKeyDown,
    renderMarkdown,
  } = useChatbot();

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

  const hasSentPromptRef = useRef(false);
  const handleSendRef = useRef(handleSend);
  handleSendRef.current = handleSend;

  useEffect(() => {
    if (isOpen && initialPrompt && !hasSentPromptRef.current) {
      handleSendRef.current(initialPrompt);
      hasSentPromptRef.current = true;
    }
  }, [isOpen, initialPrompt]);

  const askedTexts = new Set(
    messages.filter((msg) => msg.type === "user").map((msg) => msg.text),
  );
  const remainingSuggestions = SUGGESTIONS.filter((text) => !askedTexts.has(text));
  const showSuggestions = !sending && remainingSuggestions.length > 0;

  return (
    <>
      {isOpen && (
        <div className={styles.customModalBackdrop} onClick={onClose}>
          <div
            className={styles.customModalContainer}
            onClick={(e) => e.stopPropagation()}
          >
            <button className={styles.closeButton} onClick={onClose}>
              ×
            </button>
            <div className={styles.chatContainer}>
              <div className={styles.chatHeader}>
                <div className={styles.chatHeaderContent}>
                  <img
                    src="/agent.png"
                    alt="Agent"
                    className={styles.agentImage}
                  />
                  <div className={styles.headerTitleWrapper}>
                    <H2 className={styles.chatTitle}>Leafy Bank Assistant</H2>
                    <div className={styles.status}>
                      <span className={styles.pulseDot} />
                      Available
                    </div>
                  </div>
                </div>
              </div>

              <div className={styles.tabsWrapper}>
                <Tabs
                  aria-label="Chat tabs"
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
                                  : styles.assistantMessage
                            }`}
                          >
                            {msg.type === "assistant" ? (
                              <div className={styles.assistantContent}>
                                <div
                                  className={styles.messageText}
                                  dangerouslySetInnerHTML={renderMarkdown(
                                    msg.text,
                                  )}
                                />
                                <SupportingDocs docs={msg.supportingDocs} />
                              </div>
                            ) : (
                              <Body className={styles.messageText}>
                                {msg.text}
                              </Body>
                            )}
                          </div>
                        ))}

                        {showSuggestions && (
                          <div className={styles.suggestions}>
                            {remainingSuggestions.map((text, i) => (
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
                              <span>Searching documents...</span>
                            </div>
                          </div>
                        )}

                        <div ref={messagesEndRef} />
                      </div>

                      <div className={styles.chatInputContainer}>
                        <input
                          ref={inputRef}
                          type="text"
                          placeholder={
                            sending
                              ? "Searching documents..."
                              : "Ask about your banking terms..."
                          }
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
                                dangerouslySetInnerHTML={renderMarkdown(
                                  item.body,
                                )}
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
      )}
    </>
  );
}
