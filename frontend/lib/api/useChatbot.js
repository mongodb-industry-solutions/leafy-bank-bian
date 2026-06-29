"use client";

import { useUser } from "@/lib/context/UserContext";
import { marked } from "marked";
import { useState } from "react";
import { chatApi } from "./client";

marked.setOptions({ breaks: true, gfm: true });

const WELCOME_MESSAGE =
  "Hi! I'm your Leafy Bank assistant. Ask me anything about your personal banking terms, conditions, and policies.";

export function useChatbot() {
  const { selectedUser } = useUser();

  const [messages, setMessages] = useState([
    { type: "assistant", text: WELCOME_MESSAGE },
  ]);
  const [inputValue, setInputValue] = useState("");
  const [sending, setSending] = useState(false);

  async function handleSend(overrideText) {
    const text = overrideText || inputValue.trim();
    if (!text || sending) return;
    if (!selectedUser?.name) return;

    setMessages((prev) => [...prev, { type: "user", text }]);
    setInputValue("");
    setSending(true);

    const { data, error } = await chatApi("querythepdf", {
      body: {
        industry: "fsi",
        demo_name: "leafy_bank_assistant",
        query: text,
        guidelines: "personal-banking-terms-conditions.pdf",
      },
    });

    if (error) {
      setMessages((prev) => [
        ...prev,
        { type: "error", text: `Error: ${error}` },
      ]);
    } else {
      setMessages((prev) => [
        ...prev,
        {
          type: "assistant",
          text: data.answer,
          supportingDocs: data.supporting_docs || [],
        },
      ]);
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
    handleSend,
    handleKeyDown,
    renderMarkdown,
  };
}
