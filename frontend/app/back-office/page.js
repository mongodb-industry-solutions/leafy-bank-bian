"use client";

/**
 * Back-office reviewer experience for real legacy human-review suspensions
 * (human_review.py's app.suspend, suspend_reason "awaiting_human_review").
 *
 * Pending reviews are fetched live from the local `agentic dev up` stack
 * (GET /api/v1/executions?status=suspended, via
 * app/api/customer360-review/list/route.js) — a real platform record, not
 * anything this frontend tracks itself. The exact shape of each list item
 * beyond execution_id/suspend_reason/suspend_context isn't fully documented,
 * so field access below is defensive (see normalizeReview).
 *
 * Approve/Deny calls the local resume endpoint (via
 * app/api/customer360-review/resume/route.js — no credentials needed for a
 * local dev stack, unlike a real deployment's hosted API Gateway) and polls
 * for the resumed run's final result — resume is asynchronous. Once
 * resolved, the outcome is written to BACKOFFICE_DECISION_KEY for the
 * customer's tab to pick up (on next mount — see that module's docstring
 * for why this isn't a live push).
 *
 * Rendered under the app's normal root layout (NavBar + branding). NavBar's
 * isBackOffice check keeps this route looking like a clean ops screen instead
 * of showing a customer's own nav links; the reviewer identity shown here
 * comes from the real selectedUser — set automatically by the chat's "Open
 * Back-Office View" button via the same selectUser() mechanism the app uses
 * for every other backoffice persona (see LeafyBankChatAssistant.js).
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@leafygreen-ui/button";
import Card from "@leafygreen-ui/card";
import Icon from "@leafygreen-ui/icon";
import { Body, H2, H3 } from "@leafygreen-ui/typography";

import {
  getCustomer360ExecutionStatus,
  listCustomer360SuspendedReviews,
  resumeCustomer360Review,
} from "@/lib/api/client";
import { useUser } from "@/lib/context/UserContext";
import { USER_LIST } from "@/lib/constants";
import { BACKOFFICE_DECISION_KEY } from "@/lib/const/magentaBackofficeBridge";
import styles from "./page.module.css";

// Frida is the customer the original mock conversation was written for —
// switching back to her mirrors how the chat auto-selects the reviewer
// persona on the way in (see LeafyBankChatAssistant.js).
const CUSTOMER_PERSONA = USER_LIST.find((u) => u.id === "65a546ae4a8f64e8f88fb89e");

// Fields inside suspendContext.review_presentation that get their own
// dedicated treatment rather than the generic key/value grid.
const SPECIAL_FIELDS = new Set(["reason", "conversation_summary", "message", "response_schema"]);

function humanizeKey(key) {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// suspend_context.allowed_decisions isn't a guaranteed field (the platform
// contract only promises it "if present") — when present, hide whichever
// button it excludes rather than assume both are always valid.
function isDecisionAllowed(allowedDecisions, decision) {
  if (!Array.isArray(allowedDecisions) || allowedDecisions.length === 0) return true;
  const normalized = allowedDecisions.map((d) => String(d).toLowerCase());
  return normalized.some((d) => d.startsWith(decision.slice(0, 6))); // "approv"/"deni" prefix match
}

// List-item field names beyond execution_id/suspend_reason/suspend_context
// aren't documented, so read defensively rather than assume one exact shape.
function normalizeReview(item) {
  return {
    executionId: item.execution_id,
    suspendReason: item.suspend_reason,
    suspendContext: item.suspend_context || {},
    submittedAt: item.created_at || item.submitted_at || item.updated_at || null,
    userId: item.user_id || item.userId || null,
  };
}

async function pollUntilTerminal(executionId, { attempts = 10, delayMs = 1500 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const { data, error } = await getCustomer360ExecutionStatus(executionId);
    if (error) return { data: null, error };
    if (data?.status !== "resuming") return { data, error: null };
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return { data: null, error: "Timed out waiting for the execution to finish resuming." };
}

export default function BackOfficePage() {
  const { selectedUser, selectUser, markIntentionalNavigation } = useUser();
  const router = useRouter();
  const [reviews, setReviews] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [notesById, setNotesById] = useState({});
  const [resolvingId, setResolvingId] = useState(null);
  const [resolvedById, setResolvedById] = useState({});

  const refresh = useCallback(async () => {
    const { data, error } = await listCustomer360SuspendedReviews();
    if (error) return; // keep showing the last known list rather than clearing it on a blip
    const items = Array.isArray(data) ? data : data?.executions || data?.items || [];
    const pending = items.map(normalizeReview);
    setReviews(pending);
    setSelectedId((prev) => prev ?? pending[0]?.executionId ?? null);
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", refresh);
    };
  }, [refresh]);

  // Falls back to a generic reviewer if this page is opened directly (without
  // going through the chat's auto-select) rather than showing "undefined".
  const reviewerName = selectedUser?.name || "Reviewer";
  const reviewerRole = selectedUser?.role || "Back Office";
  const reviewerInitials = reviewerName
    .split(" ")
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  async function handleDecision(review, decision) {
    setResolvingId(review.executionId);

    const { data: resumeData, error: resumeError } = await resumeCustomer360Review({
      executionId: review.executionId,
      decision,
      reviewerNotes: notesById[review.executionId] || "",
    });

    if (resumeError || !resumeData?.success) {
      setResolvedById((prev) => ({
        ...prev,
        [review.executionId]: { decision, error: resumeError || "Resume request failed." },
      }));
      setResolvingId(null);
      return;
    }

    const { data: finalData, error: pollError } = await pollUntilTerminal(review.executionId);

    if (pollError || !finalData) {
      setResolvedById((prev) => ({
        ...prev,
        [review.executionId]: { decision, error: pollError || "Polling failed." },
      }));
      setResolvingId(null);
      return;
    }

    setResolvedById((prev) => ({
      ...prev,
      [review.executionId]: { decision, response: finalData.response, status: finalData.status },
    }));
    setReviews((prev) => prev.filter((r) => r.executionId !== review.executionId));
    refresh(); // reconcile against the platform's own list

    try {
      localStorage.setItem(
        BACKOFFICE_DECISION_KEY,
        JSON.stringify({ executionId: review.executionId, decision, response: finalData.response }),
      );
    } catch {
      /* the on-page outcome still shows either way */
    }

    setResolvingId(null);
  }

  // Same mechanism used to arrive here (see LeafyBankChatAssistant.openBackOffice):
  // switch the session's identity back to the customer before navigating, so
  // "/" renders as Frida again rather than staying signed in as the reviewer.
  // markIntentionalNavigation() is required here: this tab has never visited
  // "/" before, so Home()'s isFreshBrowserLoad() check hasn't fired yet in it —
  // without the breadcrumb it reads as a genuine fresh load and clearUser()
  // immediately undoes the selectUser() above, landing on the Login screen
  // instead of Frida's dashboard.
  function handleBackToCustomer() {
    markIntentionalNavigation();
    if (CUSTOMER_PERSONA) selectUser(CUSTOMER_PERSONA);
    router.push("/");
  }

  return (
    <div className={styles.container}>
      <div className={styles.topBar}>
        <button type="button" className={styles.backLink} onClick={handleBackToCustomer}>
          <Icon glyph="ArrowLeft" size="small" />
          Back to customer view
        </button>

        <div className={styles.reviewerChip}>
          <span className={styles.reviewerAvatar}>{reviewerInitials}</span>
          <div>
            <div className={styles.reviewerName}>{reviewerName}</div>
            <div className={styles.reviewerRole}>{reviewerRole}</div>
          </div>
        </div>
      </div>

      <H2 className={styles.pageTitle}>Back Office — Approvals</H2>
      <Body className={styles.pageSubtitle}>
        Requests the assistant flagged for manual review before finishing them.
      </Body>

      {reviews.length === 0 && (
        <Body className={styles.pageSubtitle}>No pending reviews right now.</Body>
      )}

      <div className={styles.requestList}>
        {reviews.map((review) => {
          const isSelected = review.executionId === selectedId;
          const resolved = resolvedById[review.executionId];
          const isResolving = resolvingId === review.executionId;
          const presentation = review.suspendContext?.review_presentation || {};
          const allowedDecisions = review.suspendContext?.allowed_decisions;
          const otherFields = Object.entries(presentation).filter(([k]) => !SPECIAL_FIELDS.has(k));

          return (
            <div key={review.executionId} className={styles.requestGroup}>
              <button
                type="button"
                className={`${styles.requestItem} ${isSelected ? styles.requestItemActive : ""}`}
                onClick={() => setSelectedId(isSelected ? null : review.executionId)}
              >
                <div className={styles.requestItemMain}>
                  <span className={styles.requestItemApplicant}>
                    {presentation.holder_name || review.userId || "Customer"}
                  </span>
                  <span className={styles.requestItemProduct}>
                    {presentation.policy_type || review.suspendReason}
                  </span>
                </div>
                <div className={styles.requestItemMeta}>
                  {review.submittedAt && (
                    <span className={styles.requestItemTime}>
                      {new Date(review.submittedAt).toLocaleString()}
                    </span>
                  )}
                  <Icon glyph={isSelected ? "ChevronUp" : "ChevronDown"} size="small" />
                </div>
              </button>

              {isSelected && (
                <Card className={styles.card}>
                  <div className={styles.cardHeader}>
                    <div className={styles.cardTitleGroup}>
                      <H3 className={styles.productName}>
                        {presentation.policy_type || "Review request"}
                      </H3>
                      <span className={styles.appId}>{review.executionId}</span>
                    </div>
                  </div>

                  <div className={styles.detailGrid}>
                    {otherFields.map(([key, value]) => (
                      <div key={key}>
                        <span className={styles.detailLabel}>{humanizeKey(key)}</span>
                        <span className={styles.detailValue}>{String(value)}</span>
                      </div>
                    ))}
                  </div>

                  {presentation.reason && (
                    <div className={styles.riskNotes}>
                      <span className={styles.riskNotesLabel}>Why this needs review</span>
                      <Body>{presentation.reason}</Body>
                    </div>
                  )}

                  {presentation.conversation_summary && (
                    <div className={styles.riskNotes}>
                      <span className={styles.riskNotesLabel}>Conversation summary</span>
                      <Body>{presentation.conversation_summary}</Body>
                    </div>
                  )}

                  {resolved ? (
                    resolved.error ? (
                      <div className={styles.decisionNote}>
                        <Icon glyph="XWithCircle" fill="#B91C1C" />
                        {resolved.error}
                      </div>
                    ) : (
                      <div className={styles.decisionNote}>
                        <Icon
                          glyph={resolved.decision === "approved" ? "CheckmarkWithCircle" : "XWithCircle"}
                          fill={resolved.decision === "approved" ? "#00684A" : "#B91C1C"}
                        />
                        Decision recorded and resumed on the agent.
                      </div>
                    )
                  ) : (
                    <>
                      <textarea
                        className={styles.notesInput}
                        placeholder="Reviewer notes (included in the resume decision)..."
                        value={notesById[review.executionId] || ""}
                        onChange={(e) =>
                          setNotesById((prev) => ({ ...prev, [review.executionId]: e.target.value }))
                        }
                        disabled={isResolving}
                      />
                      <div className={styles.actionsRow}>
                        {isDecisionAllowed(allowedDecisions, "approved") && (
                          <Button
                            variant="baseGreen"
                            leftGlyph={<Icon glyph="CheckmarkWithCircle" />}
                            disabled={isResolving}
                            onClick={() => handleDecision(review, "approved")}
                          >
                            {isResolving ? "Resuming…" : "Approve"}
                          </Button>
                        )}
                        {isDecisionAllowed(allowedDecisions, "denied") && (
                          <Button
                            variant="dangerOutline"
                            leftGlyph={<Icon glyph="XWithCircle" />}
                            disabled={isResolving}
                            onClick={() => handleDecision(review, "denied")}
                          >
                            {isResolving ? "Resuming…" : "Deny"}
                          </Button>
                        )}
                      </div>
                    </>
                  )}
                </Card>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
