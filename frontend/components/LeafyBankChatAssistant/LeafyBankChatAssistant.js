"use client";

/**
 * Real Magenta-powered assistant — calls the customer360-agent's
 * POST /invoke (proxied via app/api/customer360-chat/route.js) instead of
 * walking the old scripted CONVERSATION mock (see git history for that
 * version). UI shell (bubbles, header, input) kept as-is; the mock's
 * insights/decision-button/backoffice-hint rendering was mock-script-only
 * and is dropped since the real orchestrator drives everything through
 * plain response text.
 *
 * /invoke is synchronous, non-streaming (see PROJECT-PLAN.md): one request,
 * one JSON response — no thread_id, a `session_id` instead, and no partial
 * "thinking" steps to render (unlike the openfinance-chat SSE integration).
 *
 * A "suspended" status (human_review.py's app.suspend call) is NOT resumed
 * from here — app/back-office/page.js does that, by fetching pending
 * reviews live from the local `agentic dev up` stack rather than anything
 * this chat writes down. This chat just tells the customer their request
 * needs review; it doesn't need to track the execution any further.
 *
 * Session ownership lives in UserContext, not here: on real login,
 * selectUser() generates the session_id AND fires the orchestrator's
 * "Session Start" turn (system_message.py) automatically, before the
 * customer ever opens this chat — so financial_wellness has already run
 * and a real, personalized greeting (`chatGreeting`) is usually sitting
 * ready the instant they do. This component just consumes chatSessionId/
 * chatGreeting reactively and falls back to generating its own session id
 * (same as before) if context has none yet (e.g. a very first load with
 * no prior login, or storage unavailable).
 */

import { marked } from "marked";
import { useEffect, useRef, useState } from "react";
import { H2 } from "@leafygreen-ui/typography";

import { customer360ChatApi } from "@/lib/api/client";
import { chatHistoryKey, chatSessionIdKey } from "@/lib/const/magentaBackofficeBridge";
import { useUser } from "@/lib/context/UserContext";
import { USER_LIST } from "@/lib/constants";
import styles from "./LeafyBankChatAssistant.module.css";

// The same persona backing the mock back-office review page — see
// USER_MAP in lib/constants.js ("Noah", Section: "backoffice", Url: "/back-office").
const BACKOFFICE_REVIEWER = USER_LIST.find((u) => u.url === "/back-office");

const WELCOME_MESSAGE =
  "Hi! I'm the Leafy Bank Assistant. Ask me about insurance, or anything else I can help with.";

// Shown instead of WELCOME_MESSAGE while the Session Start greeting (see
// UserContext's selectUser) is still in flight — otherwise the static
// WELCOME_MESSAGE looks like a final answer rather than "still working on
// it," which is exactly what happened before this existed.
const GATHERING_MESSAGE =
  "Hi! I have some financial wellness advice for you based on your transaction history for the " +
  "last 6 months. Give me just a sec and I'll show it to you…";

function loadHistory(userId) {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(chatHistoryKey(userId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

marked.setOptions({ breaks: true, gfm: true });

export default function LeafyBankChatAssistant({ isOpen, onClose, pendingResolution, onResolutionConsumed }) {
  const { selectUser, selectedUser, chatSessionId, chatGreeting, chatGreetingLoading } = useUser();
  const userId = selectedUser?.id;
  // bankUsername (e.g. "gracehop") is the identifier already used for
  // backend-facing calls elsewhere (openfinance) — reused here as the
  // customer360-agent's user_id, distinct from the display name.
  const backendUserId = selectedUser?.bankUsername || selectedUser?.name;

  const [messages, setMessages] = useState(() => {
    const history = loadHistory(userId);
    if (history.length > 0) return history;
    return [{ id: 0, kind: "bot", text: chatGreetingLoading ? GATHERING_MESSAGE : WELCOME_MESSAGE }];
  });
  // Falls back to generating its own id only if UserContext has none yet
  // (e.g. storage was unavailable during login) — normally chatSessionId
  // from context (created at login, see UserContext's selectUser) is used.
  // manualSessionId overrides both once the customer hits "Restart
  // conversation" (see restart()), since context's id belongs to the
  // now-abandoned conversation.
  const fallbackSessionIdRef = useRef(
    typeof window !== "undefined" && window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `${userId ?? "anon"}-${Date.now()}`,
  );
  const [manualSessionId, setManualSessionId] = useState(null);
  const sessionId = manualSessionId || chatSessionId || fallbackSessionIdRef.current;
  const appliedGreetingRef = useRef(false);
  const [thinking, setThinking] = useState(false);
  const [inputValue, setInputValue] = useState("");

  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);
  const nextIdRef = useRef(Math.max(0, ...messages.map((m) => m.id ?? 0)));
  const resolutionAppliedRef = useRef(false);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, thinking]);

  // Persist the transcript so it survives the full-page navigation the
  // back-office "Back to customer view" link causes (a different tab/mount
  // than the one holding the live conversation).
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(chatHistoryKey(userId), JSON.stringify(messages));
    } catch {
      /* storage unavailable (private mode, quota) — chat still works, it
         just won't survive a reload */
    }
  }, [messages, userId]);

  // Apply the login-time proactive greeting (see UserContext's selectUser)
  // once it arrives — but only if this is still a fresh, untouched chat
  // (just the placeholder, id 0). If the customer already started typing
  // before the greeting resolved, don't clobber a real conversation that's
  // already underway.
  useEffect(() => {
    if (!chatGreeting || appliedGreetingRef.current) return;
    if (messages.length === 1 && messages[0].id === 0 && messages[0].kind === "bot") {
      appliedGreetingRef.current = true;
      setMessages([{ id: 0, kind: "bot", text: chatGreeting }]);
    }
  }, [chatGreeting, messages]);

  // The greeting call failed (or there was never one to begin with) —
  // stop showing GATHERING_MESSAGE and fall back to the normal static
  // welcome, but only if still untouched and nothing ever arrived.
  useEffect(() => {
    if (chatGreetingLoading || chatGreeting || appliedGreetingRef.current) return;
    if (messages.length === 1 && messages[0].id === 0 && messages[0].text === GATHERING_MESSAGE) {
      appliedGreetingRef.current = true;
      setMessages([{ id: 0, kind: "bot", text: WELCOME_MESSAGE }]);
    }
  }, [chatGreetingLoading, chatGreeting, messages]);

  // Once the back-office page actually resumes a suspended execution (not
  // just clicks Approve/Deny — only after the resumed run reaches a
  // terminal status), whichever tab next mounts as the customer picks this
  // up (see FloatingAssistant) and hands it here as a prop — append the
  // real agent response to the (resumed) conversation.
  //
  // This does NOT push a live update to an already-open customer tab that's
  // never reloaded — a deliberate scope cut (see
  // lib/const/magentaBackofficeBridge.js's module docstring), not a full
  // pub/sub.
  useEffect(() => {
    if (!pendingResolution || resolutionAppliedRef.current) return;
    resolutionAppliedRef.current = true;
    const { response, decision } = pendingResolution;
    appendMessage({
      kind: "bot",
      text:
        response ||
        (decision === "approved"
          ? "✅ Your request was approved."
          : "Your request was not approved this time."),
    });
    onResolutionConsumed?.();
  }, [pendingResolution, onResolutionConsumed]);

  useEffect(() => {
    if (isOpen) {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen]);

  function appendMessage(msg) {
    nextIdRef.current += 1;
    setMessages((prev) => [...prev, { id: nextIdRef.current, ...msg }]);
  }

  function renderMarkdown(text) {
    return { __html: marked.parse(text ?? "") };
  }

  async function handleSend(overrideText) {
    const text = overrideText ?? inputValue.trim();
    if (!text || thinking || !backendUserId) return;

    setInputValue("");
    appendMessage({ kind: "user", text });
    setThinking(true);

    const { data, error } = await customer360ChatApi({
      message: text,
      session_id: sessionId,
      user_id: backendUserId,
    });

    setThinking(false);

    // The local `agentic dev up` OE's real /invoke response shape (confirmed
    // against a live call) is { result, session_id, user_id, execution_id,
    // status } — NOT { success, response, ... } as originally assumed from
    // the hosted-platform docs. There's no `success` field locally; `status`
    // is the only outcome signal, and the text is under `result`.
    if (error) {
      appendMessage({ kind: "bot", text: `⚠️ Sorry, I ran into a problem: ${error}` });
      console.error("customer360-chat failed:", error);
      return;
    }

    if (data?.status === "completed") {
      appendMessage({ kind: "bot", text: data.result || "" });
    } else if (data?.status === "suspended") {
      // Legacy human-review suspension (human_review.py's app.suspend call,
      // suspend_reason "awaiting_human_review") — resolved via the
      // back-office page's Approve/Deny, which fetches pending reviews
      // live from the local dev stack (GET /api/v1/executions?status=
      // suspended), not from anything tracked here.
      appendMessage({
        kind: "bot",
        text: data.result || "I've submitted this for review — you'll hear back shortly.",
      });
    } else {
      appendMessage({
        kind: "bot",
        text: data?.result || `⚠️ Unexpected status from the assistant: ${data?.status ?? "no response"}`,
      });
      console.error("customer360-chat unexpected response:", data);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  // Same mechanism the app already uses to switch to a backoffice persona
  // (see UserInfo.handleSelectUser for Marc -> /gl-pipeline-monitor), just
  // triggered automatically instead of via the manual switch-user dropdown.
  // selectUser() persists to localStorage, which a freshly opened tab reads
  // unconditionally on mount, so the new tab opens already "signed in" as
  // the reviewer — no separate login step, and the customer's own chat tab
  // and session are left untouched.
  function openBackOffice() {
    if (BACKOFFICE_REVIEWER) selectUser(BACKOFFICE_REVIEWER);
    window.open("/back-office", "_blank", "noopener,noreferrer");
  }

  function restart() {
    setThinking(false);
    setMessages([{ id: 0, kind: "bot", text: WELCOME_MESSAGE }]);
    nextIdRef.current = 0;
    const newSessionId =
      typeof window !== "undefined" && window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${userId ?? "anon"}-${Date.now()}`;
    setManualSessionId(newSessionId);
    appliedGreetingRef.current = true; // don't reapply a stale greeting from the abandoned session
    resolutionAppliedRef.current = false;
    try {
      localStorage.removeItem(chatHistoryKey(userId));
      localStorage.setItem(chatSessionIdKey(userId), newSessionId);
    } catch {
      /* ignore */
    }
  }

  if (!isOpen) return null;

  return (
    <div className={styles.customModalBackdrop} onClick={onClose}>
      <div className={styles.customModalContainer} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeButton} onClick={onClose}>
          ×
        </button>

        <div className={styles.chatContainer}>
          <div className={styles.chatHeader}>
            <div className={styles.chatHeaderContent}>
              <img src="/agent.png" alt="Agent" className={styles.agentImage} />
              <div className={styles.headerTitleWrapper}>
                <H2 className={styles.chatTitle}>Leafy Bank Assistant</H2>
                <div className={styles.status}>
                  <span className={styles.pulseDot} />
                  Available
                </div>
              </div>
            </div>
          </div>

          <div className={styles.chatTabContent}>
            <div className={styles.chatMessages}>
              {messages.map((msg) => (
                <div
                  key={msg.id}
                  className={`${styles.message} ${
                    msg.kind === "user" ? styles.userMessage : styles.assistantMessage
                  }`}
                >
                  {msg.kind === "user" ? (
                    <div className={styles.messageText}>{msg.text}</div>
                  ) : (
                    <div className={styles.assistantContent}>
                      {msg.text && (
                        <div
                          className={styles.messageText}
                          dangerouslySetInnerHTML={renderMarkdown(msg.text)}
                        />
                      )}
                    </div>
                  )}
                </div>
              ))}

              {messages.length === 1 && messages[0].text === GATHERING_MESSAGE && (
                <div className={styles.stepIndicator}>
                  <div className={styles.stepHeader}>
                    <div className={styles.spinner} />
                    <span>Analyzing your accounts…</span>
                  </div>
                </div>
              )}

              {thinking && (
                <div className={styles.stepIndicator}>
                  <div className={styles.stepHeader}>
                    <div className={styles.spinner} />
                    <span>Thinking…</span>
                  </div>
                </div>
              )}

              <div className={styles.resetRow}>
                <button className={styles.resetButton} onClick={restart}>
                  ↺ Restart conversation
                </button>
                <button
                  type="button"
                  className={styles.resetButton}
                  onClick={openBackOffice}
                >
                  Open Back-Office View
                </button>
              </div>

              <div ref={messagesEndRef} />
            </div>

            <div className={styles.chatInputContainer}>
              <input
                ref={inputRef}
                type="text"
                placeholder={thinking ? "Thinking…" : "Type a message…"}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                className={styles.chatInput}
                disabled={thinking}
              />
              <button
                className={styles.sendButton}
                onClick={() => handleSend()}
                disabled={thinking || !inputValue.trim()}
              >
                Send
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
