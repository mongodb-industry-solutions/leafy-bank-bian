"use client";

/**
 * Real Agent Engine-powered assistant — calls the customer360-agent's
 * POST /invoke (proxied via app/api/customer360-chat/route.js) instead of
 * walking the old scripted CONVERSATION mock (see git history for that
 * version). UI built with the Via design system (@via-ds/components);
 * the mock's
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

import "@via-ds/components/index.css";
import { Avatar, Button, Callout, Card, Markdown, TextArea, ViaProvider } from "@via-ds/components";
import { useEffect, useRef, useState } from "react";

import { customer360ChatApi } from "@/lib/api/client";
import { chatHistoryKey, chatSessionIdKey } from "@/lib/const/agentEngineBackofficeBridge";
import { connectAllAccounts, CONNECT_INSTITUTIONS, CONSENT_DURATION_DAYS } from "@/lib/api/openFinance";
import { useUser } from "@/lib/context/UserContext";
import styles from "./LeafyBankChatAssistant.module.css";
import ChatChart from "./ChatChart";

// The same persona backing the mock back-office review page — see
// USER_MAP in lib/constants.js ("Noah", Section: "backoffice", Url: "/back-office").
const WELCOME_MESSAGE =
  "Hi! I'm the Leafy Bank Assistant. Ask me about insurance, or anything else I can help with.";

// Shown instead of WELCOME_MESSAGE while the Session Start greeting (see
// UserContext's selectUser) is still in flight — otherwise the static
// WELCOME_MESSAGE looks like a final answer rather than "still working on
// it," which is exactly what happened before this existed.
const GATHERING_MESSAGE =
  "Hi! I have some financial wellness advice for you based on your transaction history for the " +
  "last 6 months. Give me just a sec and I'll show it to you…";

const GATHERING_PHRASES = [
  "Analyzing your accounts…",
  "Reviewing your last 6 months of transactions…",
  "Preparing your summary…",
];
const THINKING_PHRASES = ["Thinking…", "Checking the details…", "Putting your answer together…"];

// Tappable openers shown while the conversation is still just the greeting.
const STARTER_PROMPTS = [
  "Where is my money going?",
  "Do I qualify for a personal loan?",
  "Get me a car insurance quote",
];

// Quick answers offered under an assistant message that ends in a question
// (the assistant is instructed to end every turn with a yes/no suggestion).
const QUICK_REPLIES = [
  { label: "Yes, please", text: "Yes, please" },
  { label: "Not now", text: "No thanks, not now" },
];

function ThinkingIndicator({ phrases }) {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setIndex((i) => (i + 1) % phrases.length), 3500);
    return () => clearInterval(timer);
  }, [phrases.length]);
  return (
    <div className={styles.thinking} role="status" aria-live="polite">
      <span className={styles.dots} aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <span key={index} className={styles.thinkingText}>
        {phrases[index]}
      </span>
    </div>
  );
}

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

// The conversation lives in this component's state and is only read from
// storage on mount, so switching customers must remount it: otherwise the
// previous customer's messages (and session) stay on screen and would even be
// saved under the new customer's history key. A new login session for the
// same customer also starts a fresh chat.
export default function LeafyBankChatAssistant(props) {
  const { selectedUser, chatSessionId } = useUser();
  return <LeafyBankChat key={`${selectedUser?.id ?? "none"}:${chatSessionId ?? ""}`} {...props} />;
}

function LeafyBankChat({ isOpen, onClose, pendingResolution, onResolutionConsumed }) {
  const { selectedUser, chatSessionId, chatGreeting, chatGreetingLoading } = useUser();
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
  // "Connect my other accounts" — a plain deterministic button, not
  // something the orchestrator asks about or drives (Agent Engine cannot call
  // any leafy-bank-bian/open-finance-next-gen endpoint — see
  // lib/api/openFinance.js). Runs entirely client-side, headless, then
  // just tells the chat what happened.
  const [connecting, setConnecting] = useState(false);
  const [consentPending, setConsentPending] = useState(false);
  // Derived from the (persisted) chat history, so it survives reloads.
  const accountsConnected = messages.some((m) => m.kind === "bot" && m.text?.startsWith("✅ Connected!"));

  const messagesEndRef = useRef(null);
  const composerRef = useRef(null);
  const nextIdRef = useRef(Math.max(0, ...messages.map((m) => m.id ?? 0)));
  // Outcomes already appended, by execution id: a customer can get several
  // over time, so this can't be a single once-only flag.
  const appliedResolutionsRef = useRef(new Set());

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
  // lib/const/agentEngineBackofficeBridge.js's module docstring), not a full
  // pub/sub.
  useEffect(() => {
    if (!pendingResolution) return;
    const resolutionKey = pendingResolution.executionId ?? "unkeyed";
    if (appliedResolutionsRef.current.has(resolutionKey)) return;
    appliedResolutionsRef.current.add(resolutionKey);
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

  // Focus the composer when the chat opens and again once a reply has arrived
  // (the field is disabled while the assistant is thinking, which drops focus).
  useEffect(() => {
    if (isOpen && !thinking) {
      setTimeout(() => composerRef.current?.querySelector("textarea")?.focus(), 100);
    }
  }, [isOpen, thinking]);

  function appendMessage(msg) {
    // Derive the id from the current list (not a separate counter): the list can
    // be replaced wholesale (history load, greeting, restart), which left a
    // counter behind and produced duplicate React keys.
    setMessages((prev) => [...prev, { id: Math.max(-1, ...prev.map((m) => m.id ?? 0)) + 1, ...msg }]);
  }

  // The agent emits visuals as ```chart fenced JSON blocks (see
  // system_message.py). Split them out so ChatChart can draw them; the
  // surrounding text stays markdown. A block that never closes (or whose
  // JSON is invalid) is not matched/rendered by ChatChart, so a bad block
  // can't break the rest of the reply.
  function splitChartBlocks(text) {
    const segments = [];
    const re = /```(chart|action)[^\n]*\n([\s\S]*?)```/g;
    let last = 0;
    let m;
    while ((m = re.exec(text ?? "")) !== null) {
      if (m.index > last) segments.push({ kind: "text", body: text.slice(last, m.index) });
      segments.push({ kind: m[1], body: m[2] });
      last = re.lastIndex;
    }
    if (last < (text ?? "").length) segments.push({ kind: "text", body: text.slice(last) });
    return segments.filter((s) => s.kind !== "text" || s.body.trim());
  }

  // ```action blocks (e.g. {"type":"connect_accounts","label":"..."}) become a
  // real button. Only known action types are rendered; the label is plain
  // text from the model, so it is shown as-is, never as HTML.
  function renderAction(body, key) {
    let action;
    try {
      action = JSON.parse(body);
    } catch {
      return null;
    }
    if (action?.type !== "connect_accounts") return null;
    // Only offered when the agent proposes it, and gone for good once accepted.
    if (accountsConnected) return null;
    return (
      <Button
        key={key}
        variant="ai"
        onPress={requestConnectConsent}
        isDisabled={connecting || consentPending}
        className={styles.actionButton}
      >
        {connecting ? "Connecting…" : String(action.label || "Connect my other accounts")}
      </Button>
    );
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

  // Step 1: the agent's button only asks for consent — nothing is connected
  // until the customer explicitly allows it in the consent card.
  function requestConnectConsent() {
    if (connecting || consentPending || !backendUserId) return;
    setConsentPending(true);
  }

  function declineConnect() {
    setConsentPending(false);
    // Tell the agent, so it acknowledges, records the preference and doesn't
    // offer the same thing again (nothing was connected).
    handleSend("No thanks, I don't want to connect my other accounts right now.");
  }

  // Step 2: explicit consent given — only now are consents created/approved
  // and the external data fetched.
  async function confirmConnect() {
    if (connecting || !backendUserId) return;
    setConsentPending(false);
    setConnecting(true);
    appendMessage({ kind: "user", text: "Yes, I allow Leafy Bank to connect my other accounts" });

    const { institutions, errors } = await connectAllAccounts(backendUserId);
    setConnecting(false);

    if (institutions.length === 0) {
      appendMessage({
        kind: "bot",
        text: `⚠️ I couldn't connect any accounts right now. ${errors.join("; ") || ""}`.trim(),
      });
      return;
    }

    const summary = institutions
      .map((inst) => {
        const accountCount = inst.accounts.length;
        const productCount = inst.products.length;
        const txnCount = inst.transactions.length;
        return (
          `**${inst.institution}**: ${accountCount} account${accountCount === 1 ? "" : "s"}, ` +
          `${productCount} product${productCount === 1 ? "" : "s"}, ${txnCount} transaction${txnCount === 1 ? "" : "s"}`
        );
      })
      .join("\n");
    const errorNote = errors.length > 0 ? `\n\n(Some institutions couldn't be connected: ${errors.join("; ")})` : "";

    appendMessage({
      kind: "bot",
      text: `✅ Connected! Here's what I found:\n\n${summary}${errorNote}\n\nUpdating your financial analysis to include these accounts…`,
    });

    // Don't make the customer ask: hand the agent the new data right away so
    // it re-runs the analysis (financial_wellness is always delegated fresh)
    // and reports the combined picture.
    handleSend(
      "I just connected my other bank accounts. Please update my financial analysis to include them — " +
        "give me the overall picture across all my accounts, then each bank separately.",
    );
  }

  // The back-office page presents itself as the reviewer on its own, so this
  // tab's login is left alone. (It used to switch the browser's saved login to
  // the reviewer, which every tab shares - that flipped this customer tab and
  // made the reviewer/customer identities swap depending on which tab wrote last.)
  function openBackOffice() {
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
    appliedResolutionsRef.current = new Set();
    try {
      localStorage.removeItem(chatHistoryKey(userId));
      localStorage.setItem(chatSessionIdKey(userId), newSessionId);
    } catch {
      /* ignore */
    }
  }

  if (!isOpen) return null;

  const lastMessage = messages[messages.length - 1];
  const lastSegments = lastMessage?.kind === "bot" ? splitChartBlocks(lastMessage.text) : [];
  const lastText = lastSegments
    .filter((seg) => seg.kind === "text")
    .map((seg) => seg.body)
    .join("\n")
    .trim();
  const isGathering = messages.length === 1 && messages[0].text === GATHERING_MESSAGE;
  const showStarters = messages.length === 1 && !isGathering && !thinking && lastMessage?.kind === "bot";
  const showQuickReplies =
    !showStarters &&
    !thinking &&
    !consentPending &&
    !isGathering &&
    lastMessage?.kind === "bot" &&
    !lastSegments.some((seg) => seg.kind === "action") &&
    lastText.endsWith("?");

  return (
    <ViaProvider>
      <div className={styles.backdrop} onClick={onClose}>
        <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
          <header className={styles.header}>
            <div className={styles.headerMain}>
              <Avatar src="/agent.png" alt="" size={40} />
              <div className={styles.headerText}>
                <div className={styles.title}>Leafy Bank Assistant</div>
                <div className={styles.status}>
                  <span className={styles.pulseDot} />
                  Available
                </div>
              </div>
            </div>
            <div className={styles.headerActions}>
              <Button variant="tertiary" size="small" onPress={restart}>
                Restart
              </Button>
              <Button variant="tertiary" size="small" onPress={openBackOffice}>
                Back-office view
              </Button>
              <button className={styles.closeButton} onClick={onClose} aria-label="Close chat">
                ×
              </button>
            </div>
          </header>

          <div className={styles.messages}>
            {messages.map((msg) =>
              msg.kind === "user" ? (
                <div key={msg.id} className={`${styles.row} ${styles.userRow}`}>
                  <div className={styles.userBubble}>{msg.text}</div>
                </div>
              ) : (
                <div key={msg.id} className={`${styles.row} ${styles.assistantRow}`}>
                  <Avatar src="/agent.png" alt="" size={32} className={styles.rowAvatar} />
                  <div className={styles.assistantBubble}>
                    {splitChartBlocks(msg.text).map((seg, i) =>
                      seg.kind === "chart" ? (
                        <ChatChart key={i} spec={seg.body} />
                      ) : seg.kind === "action" ? (
                        renderAction(seg.body, i)
                      ) : (
                        <Markdown key={i} className={styles.markdown}>
                          {seg.body}
                        </Markdown>
                      ),
                    )}
                  </div>
                </div>
              ),
            )}

            {(isGathering || thinking) && (
              <div className={`${styles.row} ${styles.assistantRow}`}>
                <Avatar src="/agent.png" alt="" size={32} className={styles.rowAvatar} />
                <div className={styles.assistantBubble}>
                  <ThinkingIndicator phrases={isGathering ? GATHERING_PHRASES : THINKING_PHRASES} />
                </div>
              </div>
            )}

            {showStarters && (
              <div className={styles.suggestions} aria-label="Suggested questions">
                {STARTER_PROMPTS.map((prompt) => (
                  <Button key={prompt} variant="primaryOutline" size="small" onPress={() => handleSend(prompt)}>
                    {prompt}
                  </Button>
                ))}
              </div>
            )}

            {showQuickReplies && (
              <div className={styles.suggestions} aria-label="Quick replies">
                {QUICK_REPLIES.map((reply, i) => (
                  <Button
                    key={reply.label}
                    variant={i === 0 ? "primaryOutline" : "tertiary"}
                    size="small"
                    onPress={() => handleSend(reply.text)}
                  >
                    {reply.label}
                  </Button>
                ))}
              </div>
            )}

            {consentPending && (
              <Card className={styles.consentCard} role="dialog" aria-label="Connect your other accounts">
                <div className={styles.consentTitle}>Allow Leafy Bank to connect your other accounts?</div>
                <p className={styles.consentText}>
                  I'll read data from {CONNECT_INSTITUTIONS.join(" and ")} to include them in your financial
                  analysis:
                </p>
                <ul className={styles.consentList}>
                  <li>Accounts and balances</li>
                  <li>Transactions</li>
                  <li>Loans and products</li>
                </ul>
                <Callout variant="note">
                  Read-only — I can't move money. Access expires after {CONSENT_DURATION_DAYS} days.
                </Callout>
                <div className={styles.consentActions}>
                  <Button variant="primary" onPress={confirmConnect}>
                    Allow access
                  </Button>
                  <Button variant="tertiary" onPress={declineConnect}>
                    Not now
                  </Button>
                </div>
              </Card>
            )}

            <div ref={messagesEndRef} />
          </div>

          <div className={styles.composerWrap}>
            <div className={styles.composer} ref={composerRef}>
              <TextArea
                aria-label="Message the assistant"
                placeholder={thinking ? "Thinking…" : "Ask about your finances, loans or insurance…"}
                value={inputValue}
                onChange={setInputValue}
                onKeyDown={handleKeyDown}
                isDisabled={thinking}
                className={styles.composerInput}
              />
              <Button
                variant="ai"
                onPress={() => handleSend()}
                isDisabled={thinking || !inputValue.trim()}
                className={styles.sendButton}
              >
                Send
              </Button>
            </div>
            <div className={styles.disclaimer}>
              AI assistant — it can make mistakes. Loan and insurance decisions are made by Leafy Bank.
            </div>
          </div>
        </div>
      </div>
    </ViaProvider>
  );
}
