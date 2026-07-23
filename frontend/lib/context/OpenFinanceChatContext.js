"use client";

import { openFinanceChatStream } from "@/lib/api/client";
import { useUser } from "@/lib/context/UserContext";
import { marked } from "marked";
import { createContext, useContext, useEffect, useRef, useState } from "react";

marked.setOptions({ breaks: true, gfm: true });

const WELCOME_MESSAGE =
  "Hello! 👋 Welcome to Leafy Bank's Open Banking platform.\n\n" +
  "I'm here to help you connect your external bank or fintech accounts securely and " +
  "understand exactly what data you're sharing and why.\n\n" +
  "What would you like to do today?";

// Initial recommendation chips shown alongside the welcome message.
const WELCOME_SUGGESTIONS = [
  "Connect a bank for personalized financial advice",
  "I'd like to connect my bank accounts to get complete view of my finances",
];

const OpenFinanceChatContext = createContext(null);

/**
 * Holds the Open Banking advisor conversation above the route tree so it
 * survives panel close AND navigation between pages. The backend keeps the
 * agent thread (LangGraph thread_id); persisting that id here is what lets the
 * conversation resume instead of restarting from the welcome message.
 */
export function OpenFinanceChatProvider({ children }) {
  const { selectedUser } = useUser();
  const [messages, setMessages] = useState([
    { type: "assistant", text: WELCOME_MESSAGE },
  ]);
  const [inputValue, setInputValue] = useState("");
  const [sending, setSending] = useState(false);
  const [suggestions, setSuggestions] = useState(WELCOME_SUGGESTIONS);
  const threadIdRef = useRef(null);
  const seenBroadcastsRef = useRef(new Set());

  // Reset the conversation when the logged-in user changes so one user's
  // advisor thread never leaks into another's session.
  useEffect(() => {
    setMessages([{ type: "assistant", text: WELCOME_MESSAGE }]);
    setInputValue("");
    setSuggestions(WELCOME_SUGGESTIONS);
    threadIdRef.current = null;
    seenBroadcastsRef.current = new Set();
  }, [selectedUser?.name]);

  // The bank-login tab runs the login → consent → resume flow, then broadcasts
  // the agent's post-consent reply on "leafy-bank-consent". Render it here so
  // the chat advances past the "action required" interrupt bubble.
  useEffect(() => {
    const channel = new BroadcastChannel("leafy-bank-consent");

    channel.onmessage = (event) => {
      const msg = event.data;
      if (
        msg?.type !== "consent_complete" &&
        msg?.type !== "consent_declined"
      ) {
        return;
      }
      if (msg._broadcastId && seenBroadcastsRef.current.has(msg._broadcastId)) {
        return;
      }
      if (msg._broadcastId) seenBroadcastsRef.current.add(msg._broadcastId);

      setMessages((prev) => {
        // Replace the trailing interrupt bubble with the agent's reply.
        const next = prev.filter((m) => m.type !== "interrupt");
        if (msg.response) {
          next.push({ type: "assistant", text: msg.response });
        }
        return next;
      });
      setSuggestions(msg.suggestions?.length ? msg.suggestions : []);
    };

    return () => channel.close();
  }, []);

  // Replace the trailing in-flight assistant bubble (the one accumulating the
  // tool trace) with an updater applied to its current value.
  function updateStreamingMessage(updater) {
    setMessages((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].type === "assistant" && next[i].streaming) {
          next[i] = updater({ ...next[i] });
          return next;
        }
      }
      return next;
    });
  }

  async function handleSend(overrideText) {
    const text = overrideText || inputValue.trim();
    if (!text || sending) return;
    if (!selectedUser?.name) return;

    // Add the user turn plus an empty assistant bubble that collects tool
    // steps as they stream in, then fills with the final response text.
    setMessages((prev) => [
      ...prev,
      { type: "user", text },
      { type: "assistant", text: "", steps: [], streaming: true },
    ]);
    setInputValue("");
    setSending(true);
    setSuggestions([]);

    try {
      const res = await openFinanceChatStream("chat/stream", {
        thread_id: threadIdRef.current,
        user_id: selectedUser.bankUsername,
        message: text,
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let interrupted = false;
      let gotResponse = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          for (const line of part.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            let event;
            try {
              event = JSON.parse(line.slice(6));
            } catch {
              continue;
            }
            const p = event.payload || {};

            switch (event.type) {
              case "thread_id":
                if (p.thread_id) threadIdRef.current = p.thread_id;
                break;

              case "status":
                updateStreamingMessage((m) => {
                  m.steps = [...m.steps, { kind: "status", message: p.message }];
                  return m;
                });
                break;

              case "tool_call":
                updateStreamingMessage((m) => {
                  m.steps = [
                    ...m.steps,
                    {
                      kind: "tool",
                      agent: p.agent_display || p.agent,
                      tool: p.tool,
                      args: p.args,
                      mongodbFeature: p.mongodb_feature,
                    },
                  ];
                  return m;
                });
                break;

              case "tool_result":
                // Attach the result to the most recent matching tool step.
                updateStreamingMessage((m) => {
                  const steps = [...m.steps];
                  for (let i = steps.length - 1; i >= 0; i--) {
                    if (steps[i].kind === "tool" && steps[i].tool === p.tool) {
                      steps[i] = { ...steps[i], result: p.summary };
                      break;
                    }
                  }
                  m.steps = steps;
                  return m;
                });
                break;

              case "interrupt":
                interrupted = true;
                if (p.type === "BANK_LOGIN") {
                  const params = new URLSearchParams({
                    consent_id: p.consent_id || "",
                    institution_name: p.institution_name || "",
                    thread_id: threadIdRef.current || "",
                  });
                  window.open(`/bank-login?${params.toString()}`, "_blank");
                }
                // Keep the assistant bubble (with its Thinking panel) and add a
                // SEPARATE interrupt notice. The resume handler removes only the
                // interrupt bubble, so the tool trace survives the round-trip.
                setMessages((prev) => {
                  const next = [...prev];
                  for (let i = next.length - 1; i >= 0; i--) {
                    if (next[i].type === "assistant" && next[i].streaming) {
                      if (next[i].steps?.length) {
                        next[i] = { ...next[i], streaming: false };
                      } else {
                        next.splice(i, 1); // drop the empty placeholder bubble
                      }
                      break;
                    }
                  }
                  next.push({
                    type: "interrupt",
                    text:
                      p.message ||
                      "Action required — please follow the steps in your browser.",
                  });
                  return next;
                });
                break;

              case "response":
                gotResponse = true;
                updateStreamingMessage((m) => {
                  m.text = p.text || "";
                  m.streaming = false;
                  return m;
                });
                break;

              case "suggestions":
                if (p.items?.length) setSuggestions(p.items);
                break;

              case "error":
                updateStreamingMessage((m) => {
                  m.type = "error";
                  m.streaming = false;
                  m.text = `Error: ${p.message || "stream failed"}`;
                  return m;
                });
                break;

              default:
                break;
            }
          }
        }
      }

      // Stream ended without a terminal event — clean up the empty bubble.
      if (!interrupted && !gotResponse) {
        updateStreamingMessage((m) => {
          m.streaming = false;
          if (!m.text && (!m.steps || m.steps.length === 0)) {
            m.type = "error";
            m.text = "No response received.";
          }
          return m;
        });
      }
    } catch (e) {
      updateStreamingMessage((m) => {
        m.type = "error";
        m.streaming = false;
        m.text = `Error: ${e.message}`;
        return m;
      });
    }

    setSending(false);
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function renderMarkdown(text) {
    const markdown = Array.isArray(text) ? text.join("\n\n") : text;
    return { __html: marked.parse(markdown) };
  }

  const value = {
    messages,
    inputValue,
    setInputValue,
    sending,
    suggestions,
    handleSend,
    handleKeyDown,
    renderMarkdown,
  };

  return (
    <OpenFinanceChatContext.Provider value={value}>
      {children}
    </OpenFinanceChatContext.Provider>
  );
}

export function useOpenFinanceChat() {
  const ctx = useContext(OpenFinanceChatContext);
  if (!ctx) {
    throw new Error(
      "useOpenFinanceChat must be used within an OpenFinanceChatProvider",
    );
  }
  return ctx;
}
