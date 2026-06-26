"use client";

import { useChatbot } from "@/lib/api/useChatbot";
import { TALK_TRACK } from "@/lib/const/talkTrack";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";

import { Tab, Tabs } from "@leafygreen-ui/tabs";
import { Body, H2, H3 } from "@leafygreen-ui/typography";
import { useEffect, useRef, useState } from "react";

import styles from "./LeafyBankAssistant.module.css";

const SUGGESTIONS = [
  "What can you tell me about my Leafy Bank data?",
  "I want to port my vehicle loan to a better rate",
  "I want to port my payroll deductible loan to a better rate",
  "I want to port my personal loan to a better rate",
  "I want financial advice",
];


/**
 * Renders a single step detail item (tool call or progress sub-step).
 * Used by both active step indicator and finalized "steps" messages.
 */
function StepDetailItem({ detail }) {
  const [expanded, setExpanded] = useState(false);
  const hasIO =
    (detail.kind === "tool" &&
      ((detail.args && Object.keys(detail.args).length > 0) ||
        detail.summary)) ||
    (detail.kind === "progress" && (detail.input || detail.output));

    const hasMongoFeature = !!detail.mongodbFeature;


  return (
    <div className={styles.stepDetail}>
      {hasMongoFeature && (

        <div className={styles.featureHint}>
          <Icon glyph="InfoWithCircle" size="small" />
          <Body className={styles.featureHintText}>
            Take a look at the MongoDB features used to retrieve this answer — click on
            the feature names to explore how each step works.
          </Body>
        </div>
      )}

      <div
        className={styles.stepDetailHeader}
        onClick={hasIO ? () => setExpanded((v) => !v) : undefined}
        style={hasIO ? { cursor: "pointer" } : undefined}
      >

        <span
          className={
            detail.status === "done" ? styles.iconDone : styles.iconPending
          }
        >
          {detail.status === "done" ? "✓" : "●"}
        </span>
        {detail.kind === "tool" ? (
          <>

            <code>{detail.tool}()</code>

            {detail.mongodbFeature && (
              <span className={styles.mongodbFeature}>
                {detail.mongodbFeature}
              </span>
            )}
          </>
        ) : (
          <>
            <span>{detail.message}</span>
            {detail.mongodbFeature && (
              <span className={styles.mongodbFeature}>
                {detail.mongodbFeature}
              </span>
            )}
          </>
        )}
        {hasIO && (
          <span className={styles.expandToggle}>{expanded ? "▾" : "▸"}</span>
        )}
      </div>

      {expanded && detail.kind === "tool" && (
        <div className={styles.toolIO}>
          {detail.args && Object.keys(detail.args).length > 0 && (
            <>
              <div className={styles.toolLabelInput}>input</div>
              <pre className={styles.toolInput}>
                {JSON.stringify(detail.args, null, 2)}
              </pre>
            </>
          )}
          {detail.summary && (
            <>
              <div className={styles.toolLabelOutput}>output</div>
              <pre className={styles.toolOutput}>
                {formatJSON(detail.summary)}
              </pre>
            </>
          )}
        </div>
      )}

      {expanded && detail.kind === "progress" && (
        <div className={styles.toolIO}>
          {detail.input && (
            <>
              <div className={styles.toolLabelInput}>input</div>
              <pre className={styles.toolInput}>{formatJSON(detail.input)}</pre>
            </>
          )}
          {detail.output && (
            <>
              <div className={styles.toolLabelOutput}>output</div>
              <pre className={styles.toolOutput}>
                {formatJSON(detail.output)}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Safely stringify any value for display. Handles objects, arrays, and strings. */
function formatJSON(val) {
  if (!val) return "";
  // Already an object/array — stringify directly
  if (typeof val === "object") {
    return JSON.stringify(val, null, 2);
  }
  // String — try to pretty-print if valid JSON, otherwise return as-is
  try {
    return JSON.stringify(JSON.parse(val), null, 2);
  } catch {
    return val;
  }
}

export default function LeafyBankAssistant({ isOpen, onClose, initialPrompt }) {
  const {
    messages,
    inputValue,
    setInputValue,
    sending,
    waitingForBankLogin,
    stepIndicator,
    interrupt,
    showSuggestions,
    suggestions,
    threadId,
    handleSend,
    handleResume,
    handleKeyDown,
    renderMarkdown,
  } = useChatbot();

  //Info Tabs
  const [activeTab, setActiveTab] = useState(0);

  // DOM refs
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, stepIndicator, interrupt, waitingForBankLogin]);

  // Focus input when modal opens
  useEffect(() => {
    if (isOpen) {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen]);

  //Initial prompt handling (for contextual entry points)

  const hasSentPromptRef = useRef(false);
  const handleSendRef = useRef(handleSend);
  handleSendRef.current = handleSend;

  useEffect(() => {
    if (isOpen && initialPrompt && !hasSentPromptRef.current) {
      handleSendRef.current(initialPrompt);
      hasSentPromptRef.current = true;
    }
  }, [isOpen, initialPrompt]);

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
                {threadId && (
                  <span className={styles.threadBadge}>
                    Thread: {threadId.substring(0, 8)}...
                  </span>
                )}
              </div>

              <div className={styles.encryptionBanner}>
                <span className={styles.encryptionIcon}>🔒</span>
                Agent behavior protected by{" "}
                <strong>Queryable Encryption</strong>
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
                        {messages.map((msg, i) => {
                          // Finalized step details (persisted in conversation history)
                          if (msg.type === "steps") {
                            return (
                              <div key={i} className={styles.stepsMessage}>
                                <div className={styles.stepDetails}>
                                  {msg.details.map((d, j) => (
                                    <StepDetailItem key={j} detail={d} />
                                  ))}
                                </div>
                              </div>
                            );
                          }

                          return (
                            <div
                              key={i}
                              className={`${styles.message} ${msg.type === "user"
                                ? styles.userMessage
                                : msg.type === "error"
                                  ? styles.errorMessage
                                  : styles.assistantMessage
                                }`}
                            >
                              {msg.type === "assistant" ? (
                                <div
                                  className={styles.messageText}
                                  dangerouslySetInnerHTML={renderMarkdown(
                                    msg.text,
                                  )}
                                />
                              ) : (
                                <Body className={styles.messageText}>
                                  {msg.text}
                                </Body>
                              )}
                            </div>
                          );
                        })}

                        {/* Suggestion Chips */}
                        {showSuggestions && (
                          <div className={styles.suggestions}>
                            {(suggestions || SUGGESTIONS).map((text, i) => (
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

                        {/* Active Step Indicator (while processing) */}
                        {stepIndicator && (
                          <div className={styles.stepIndicator}>
                            <div className={styles.stepHeader}>
                              <div className={styles.spinner} />
                              <span>{stepIndicator.text}</span>
                            </div>
                            {stepIndicator.details?.length > 0 && (
                              <div className={styles.stepDetails}>
                                {stepIndicator.details.map((d, i) => (
                                  <StepDetailItem key={i} detail={d} />
                                ))}
                              </div>
                            )}
                          </div>
                        )}

                        {/* Waiting for bank login in other tab */}
                        {waitingForBankLogin && (
                          <div className={styles.waitingState}>
                            <div className={styles.stepHeader}>
                              <div className={styles.spinner} />
                              <span>
                                Waiting for bank login in the other tab...
                              </span>
                            </div>
                            <Body className={styles.waitingHint}>
                              Complete the login and consent approval in the new
                              tab. This chat will update automatically.
                            </Body>
                          </div>
                        )}

                        {/* Interrupt Box — only CONSENT_APPROVAL shown inline */}
                        {interrupt && interrupt.type === "CONSENT_APPROVAL" && (
                          <div className={styles.interruptBox}>
                            <h4 className={styles.interruptTitle}>
                              Consent Approval Required
                            </h4>
                            <p className={styles.interruptMessage}>
                              {interrupt.message ||
                                "Do you approve this data-sharing consent?"}
                            </p>
                            {interrupt.permissions && (
                              <div className={styles.interruptDetails}>
                                <strong>Permissions:</strong>{" "}
                                {interrupt.permissions.join(", ")}
                              </div>
                            )}
                            {interrupt.purpose && (
                              <div className={styles.interruptDetails}>
                                <strong>Purpose:</strong> {interrupt.purpose}
                              </div>
                            )}
                            {interrupt.source_institution && (
                              <div className={styles.interruptDetails}>
                                <strong>Source:</strong>{" "}
                                {interrupt.source_institution}
                              </div>
                            )}
                            {(interrupt.display_expiration ||
                              interrupt.expiration) && (
                              <div className={styles.interruptDetails}>
                                <strong>Expires:</strong>{" "}
                                {interrupt.display_expiration ||
                                  interrupt.expiration}
                              </div>
                            )}
                            <div className={styles.interruptActions}>
                              <Button
                                variant="primary"
                                onClick={() => handleResume(true)}
                              >
                                Approve Consent
                              </Button>
                              <Button
                                variant="default"
                                onClick={() => handleResume(false)}
                              >
                                Decline
                              </Button>
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
                            waitingForBankLogin
                              ? "Waiting for bank login..."
                              : sending
                                ? "Waiting for response..."
                                : "Type your message..."
                          }
                          value={inputValue}
                          onChange={(e) => setInputValue(e.target.value)}
                          onKeyDown={handleKeyDown}
                          className={styles.chatInput}
                          disabled={sending || waitingForBankLogin}
                        />
                        <Button
                          variant="primary"
                          onClick={() => handleSend()}
                          disabled={
                            sending || waitingForBankLogin || !inputValue.trim()
                          }
                        >
                          Send
                        </Button>
                      </div>
                    </div>
                  </Tab>

                  {/* Map over TALK_TRACK for the other tabs */}
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
