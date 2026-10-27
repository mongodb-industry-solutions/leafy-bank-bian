/**
 * Cross-tab handoff between the chat (LeafyBankChatAssistant) and the back-office
 * review page for the *outcome* of a review. There's no live-push channel
 * this frontend can use to reach an already-open customer tab, so this is
 * localStorage read once on mount rather than a real notification system —
 * the back-office page opens in its own tab/window (see LeafyBankChatAssistant's
 * openBackOffice()), so an in-memory callback can't reach it either.
 *
 * Discovering *which* executions are pending review does NOT go through
 * here — the back-office page fetches that live from the local
 * `agentic dev up` stack (GET /api/v1/executions?status=suspended, via
 * app/api/customer360-review/list/route.js), since that's a real platform
 * record and this frontend shouldn't try to shadow it.
 */

// Written by back-office/page.js once a reviewer's decision has actually
// been resumed on the backend (not just clicked) and the resumed execution
// reached a terminal status. Read once by FloatingAssistant on mount to
// decide whether to auto-open the chat, and by LeafyBankChatAssistant to append
// the real agent response, then cleared.
export const BACKOFFICE_DECISION_KEY = "agentEngineBackofficeDecision";

// The conversation transcript, persisted so it survives the full page
// navigation back-office's "Back to customer view" causes (a different tab,
// and possibly a different UserProvider instance, than the one that had the
// live conversation). Namespaced per user so switching personas doesn't leak
// one customer's conversation into another's.
export function chatHistoryKey(userId) {
  return `agentEngineChatHistory:${userId ?? "anon"}`;
}

// The customer360-agent session_id, persisted alongside the transcript so a
// resumed conversation continues the same backend session rather than
// starting a fresh one on every reload.
export function chatSessionIdKey(userId) {
  return `agentEngineChatSessionId:${userId ?? "anon"}`;
}

// Execution ids of review outcomes already shown to this customer, so the
// outcome poll (FloatingAssistant) never repeats one — and so the outcomes
// that existed before first load aren't replayed as news.
export function seenOutcomesKey(userId) {
  return `agentEngineSeenReviewOutcomes:${userId ?? "anon"}`;
}
