"use client";

import { useOpenFinanceChat } from "@/lib/context/OpenFinanceChatContext";

// The Open Finance advisor conversation is held in OpenFinanceChatProvider
// (mounted above the route tree) so it survives panel close and navigation.
// This hook is a thin consumer kept for backwards compatibility.
export function useOpenFinanceChatbot() {
  return useOpenFinanceChat();
}
