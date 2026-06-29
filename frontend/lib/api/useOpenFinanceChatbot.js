"use client";

import { useUser } from "@/lib/context/UserContext";
import { marked } from "marked";
import { useRef, useState } from "react";
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
