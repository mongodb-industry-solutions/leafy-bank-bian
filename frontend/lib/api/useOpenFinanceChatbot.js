"use client";

import { useUser } from "@/lib/context/UserContext";
import { marked } from "marked";
import { useEffect, useRef, useState } from "react";
import { openFinanceChatApi } from "./client";

marked.setOptions({ breaks: true, gfm: true });

const WELCOME_MESSAGE =
  "Hi! I'm your Open Finance advisor. I can help you aggregate accounts from other banks and get a complete picture of your finances.";

export function useOpenFinanceChatbot() {
  const { selectedUser } = useUser();
  const [messages, setMessages] = useState([
    { type: "assistant", text: WELCOME_MESSAGE },
  ]);
  const [inputValue, setInputValue] = useState("");
  const [sending, setSending] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const threadIdRef = useRef(null);
  const seenBroadcastsRef = useRef(new Set());

  // The bank-login tab runs the login → consent → resume flow, then broadcasts
  // the agent's post-consent reply on "leafy-bank-consent". Render it here so
  // the chat advances past the "action required" interrupt bubble.
  useEffect(() => {
    const channel = new BroadcastChannel("leafy-bank-consent");

    channel.onmessage = (event) => {
      const msg = event.data;
      if (msg?.type !== "consent_complete" && msg?.type !== "consent_declined") {
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

  async function handleSend(overrideText) {
    const text = overrideText || inputValue.trim();
    if (!text || sending) return;
    if (!selectedUser?.name) return;

    setMessages((prev) => [...prev, { type: "user", text }]);
    setInputValue("");
    setSending(true);
    setSuggestions([]);

    const { data, error } = await openFinanceChatApi("chat", {
      body: {
        thread_id: threadIdRef.current,
        user_id: selectedUser.name,
        message: text,
      },
    });

    if (error) {
      setMessages((prev) => [
        ...prev,
        { type: "error", text: `Error: ${error}` },
      ]);
    } else {
      threadIdRef.current = data.thread_id;

      if (data.interrupt) {
        if (data.interrupt.type === "BANK_LOGIN") {
          const params = new URLSearchParams({
            consent_id: data.interrupt.consent_id || "",
            institution_name: data.interrupt.institution_name || "",
            thread_id: data.thread_id || "",
          });
          window.open(`/bank-login?${params.toString()}`, "_blank");
        }
        setMessages((prev) => [
          ...prev,
          {
            type: "interrupt",
            text:
              data.interrupt.message ||
              "Action required — please follow the steps in your browser.",
          },
        ]);
      } else if (data.response) {
        setMessages((prev) => [
          ...prev,
          { type: "assistant", text: data.response },
        ]);
        if (data.suggestions?.length) {
          setSuggestions(data.suggestions);
        }
      }
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

  return {
    messages,
    inputValue,
    setInputValue,
    sending,
    suggestions,
    handleSend,
    handleKeyDown,
    renderMarkdown,
  };
}
