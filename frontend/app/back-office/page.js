"use client";

/**
 * Back-office "approvals desk" for human-review suspensions
 * (app.suspend, suspend_reason "awaiting_human_review"), built on the Via
 * design system.
 *
 * Layout: a stats strip on top; a queue on the left (Pending / Approved /
 * Denied / All, with search); the selected request on the right.
 *
 * Data:
 *  - Pending reviews come live from the agent engine
 *    (app/api/customer360-review/list/route.js) — a real platform record, not
 *    anything this frontend tracks. Field access is defensive (see
 *    normalizeReview / reviewPresentation).
 *  - Decided reviews (loans and insurance purchases) come from the loan and
 *    quote records via the open-finance backend, since the engine keeps neither
 *    the details nor the reviewer's notes of a finished run.
 *
 * Approve/Deny resumes the execution (app/api/customer360-review/resume/
 * route.js) and polls for the resumed run's result — resume is asynchronous.
 * The customer's chat picks the outcome up itself (see FloatingAssistant);
 * BACKOFFICE_DECISION_KEY is only a same-browser shortcut.
 *
 * The page always presents the back-office reviewer (Noah), whatever the
 * browser's saved login says — that login is shared by every tab.
 */

import "@via-ds/components/index.css";
import { Avatar, Callout, SegmentedControl, SegmentedControlItem, ViaProvider } from "@via-ds/components";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import {
  getCustomer360ExecutionStatus,
  listCustomer360SuspendedReviews,
  listInsuranceReviewHistory,
  listLoanReviewHistory,
  resumeCustomer360Review,
} from "@/lib/api/client";
import { useUser } from "@/lib/context/UserContext";
import { USER_LIST } from "@/lib/constants";
import { BACKOFFICE_DECISION_KEY } from "@/lib/const/agentEngineBackofficeBridge";
import {
  DetailPanel,
  initialsOf,
  itemFromHistory,
  itemFromPending,
  KpiTile,
  normalizeReview,
  QueueItem,
  timeAgo,
} from "@/components/BackOffice/BackOfficeParts";
import styles from "@/components/BackOffice/BackOffice.module.css";

// Frida is the customer the original mock conversation was written for —
// the fallback persona when "Back to customer view" finds a back-office login.
const CUSTOMER_PERSONA = USER_LIST.find((u) => u.id === "65a546ae4a8f64e8f88fb89e");

// This page is always the back-office reviewer (Noah), whatever the browser's
// saved login says: that login is shared by every tab, so a customer tab (or
// "Back to customer view") writing it must not change who the reviewer is.
const BACKOFFICE_REVIEWER = USER_LIST.find((u) => u.url === "/back-office");

const FILTERS = [
  { id: "pending", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "denied", label: "Denied" },
  { id: "all", label: "All" },
];

// Resume is asynchronous and the resumed run (record the decision, pay out,
// write the customer message) takes a while, so poll until the run reaches a
// terminal status. Right after the resume call the engine may still report
// "suspended" for a moment, so only these count as finished.
const TERMINAL_STATUSES = new Set(["completed", "error", "failed", "cancelled"]);

async function pollUntilTerminal(executionId, { attempts = 60, delayMs = 2000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    const { data, error } = await getCustomer360ExecutionStatus(executionId);
    if (error) continue; // a blip while the run is busy — keep waiting
    if (TERMINAL_STATUSES.has(data?.status)) return { data, error: null };
  }
  return { data: null, error: null, timedOut: true };
}

// ── page ─────────────────────────────────────────────────────────────

export default function BackOfficePage() {
  const { selectedUser, selectUser, markIntentionalNavigation } = useUser();
  const router = useRouter();
  const [reviews, setReviews] = useState([]);
  const [history, setHistory] = useState([]);
  const [historyError, setHistoryError] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [selectedKey, setSelectedKey] = useState(null);
  const [filter, setFilter] = useState("pending");
  const [query, setQuery] = useState("");
  const [notesById, setNotesById] = useState({});
  const [resolvingId, setResolvingId] = useState(null);
  const [resolvedById, setResolvedById] = useState({});
  // The review leaves the pending list once it's no longer suspended, so the
  // outcome of the last decision is shown here instead.
  const [notice, setNotice] = useState(null);

  const refresh = useCallback(async () => {
    const { data, error } = await listCustomer360SuspendedReviews();
    if (!error) {
      const items = Array.isArray(data) ? data : data?.executions || data?.items || [];
      setReviews(items.map(normalizeReview));
    } // keep showing the last known list rather than clearing it on a blip

    // Decided reviews come from the loan/quote records, not the agent engine
    // (which only keeps recent runs); a failure here must not hide the pending list.
    const [loans, insurance] = await Promise.all([listLoanReviewHistory(), listInsuranceReviewHistory()]);
    if (loans.error || insurance.error) {
      setHistoryError(
        "Couldn't load past decisions. Is the open-finance backend running (and restarted since the last update)?",
      );
    } else {
      setHistoryError(null);
      setHistory(
        [
          ...(loans.data?.reviews || []).map((r) => ({ ...r, kind: "loan", key: `loan:${r.loan_number}` })),
          ...(insurance.data?.reviews || []).map((r) => ({ ...r, kind: "insurance", key: `insurance:${r.quote_id}` })),
        ].sort((a, b) => String(b.decided_at || "").localeCompare(String(a.decided_at || ""))),
      );
    }
    setLoaded(true);
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

  const pendingItems = useMemo(() => reviews.map(itemFromPending), [reviews]);
  const historyItems = useMemo(() => history.map(itemFromHistory), [history]);

  const counts = {
    pending: pendingItems.length,
    approved: historyItems.filter((i) => i.status === "approved").length,
    denied: historyItems.filter((i) => i.status === "denied").length,
    all: pendingItems.length + historyItems.length,
  };

  const visible = useMemo(() => {
    const pool =
      filter === "pending"
        ? pendingItems
        : filter === "all"
          ? [...pendingItems, ...historyItems]
          : historyItems.filter((i) => i.status === filter);
    const q = query.trim().toLowerCase();
    if (!q) return pool;
    return pool.filter((i) =>
      [i.customer, i.username, i.title, i.reference].some((field) => String(field || "").toLowerCase().includes(q)),
    );
  }, [filter, pendingItems, historyItems, query]);

  const selected = visible.find((i) => i.key === selectedKey) || visible[0] || null;

  // Headline numbers for the strip: what needs doing, how decisions are going,
  // and what is approved but not yet delivered.
  const decided = counts.approved + counts.denied;
  const approvedLoanValue = historyItems
    .filter((i) => i.kind === "loan" && i.status === "approved")
    .reduce((sum, i) => sum + (Number(i.h.amount) || 0), 0);
  const loanCurrency = historyItems.find((i) => i.kind === "loan")?.h.currency || "";
  const undelivered = historyItems.filter(
    (i) => i.status === "approved" && (i.kind === "loan" ? !i.h.funded : !i.h.policy_created),
  ).length;
  const oldestPending = pendingItems
    .map((i) => i.when)
    .filter(Boolean)
    .sort()[0];

  // Falls back to the reviewer persona so this page never shows "undefined".
  const reviewer = selectedUser?.url === "/back-office" ? selectedUser : BACKOFFICE_REVIEWER;
  const reviewerName = reviewer?.name || "Reviewer";
  const reviewerRole = reviewer?.role || "Back Office";

  async function handleDecision(review, decision) {
    setResolvingId(review.executionId);
    setResolvedById((prev) => ({ ...prev, [review.executionId]: undefined }));

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

    const { data: finalData, timedOut } = await pollUntilTerminal(review.executionId);

    // The decision was delivered the moment resume was accepted; polling only
    // waits for the agent to finish telling the customer. Timing out, or the
    // run ending in an error afterwards, must not read as "the decision failed".
    const failedAfterResume = finalData && finalData.status !== "completed";
    setReviews((prev) => prev.filter((r) => r.executionId !== review.executionId));
    refresh(); // reconcile against the platform's own list

    const customerName = review.suspendContext?.holder_name || review.userId || "the customer";
    setNotice({
      kind: timedOut || failedAfterResume ? "warning" : "success",
      text:
        timedOut || failedAfterResume
          ? `${customerName}: ${decision === "approved" ? "approved" : "denied"}. ${
              timedOut
                ? "The assistant is still finishing up; the customer will be notified when it completes."
                : `The follow-up run ended with status "${finalData.status}" - check the agent logs.`
            }`
          : `${customerName}: ${decision === "approved" ? "approved" : "denied"}. The customer has been notified in their chat.`,
    });

    // Same-browser shortcut: open the customer's chat on the result straight
    // away. Any other browser/session gets it from the customer chat's own
    // outcome poll (see FloatingAssistant), so skip this unless there is a
    // real message to show.
    if (finalData?.result) {
      try {
        localStorage.setItem(
          BACKOFFICE_DECISION_KEY,
          JSON.stringify({ executionId: review.executionId, decision, response: finalData.result }),
        );
      } catch {
        /* the on-page outcome still shows either way */
      }
    }

    setResolvingId(null);
  }

  // markIntentionalNavigation() is required here: this tab has never visited
  // "/" before, so Home()'s isFreshBrowserLoad() check hasn't fired yet in it —
  // without the breadcrumb it reads as a genuine fresh load and clearUser()
  // immediately undoes any selectUser() below, landing on the Login screen
  // instead of the customer's dashboard.
  function handleBackToCustomer() {
    markIntentionalNavigation();
    // Only switch identity if the saved login is actually a back-office persona
    // (e.g. left over from an older flow); a customer's own login (Frida, Grace)
    // is kept as is, not replaced by a hardcoded one.
    if (CUSTOMER_PERSONA && selectedUser?.section === "backoffice") selectUser(CUSTOMER_PERSONA);
    router.push("/");
  }

  const emptyText =
    filter === "pending"
      ? "You're all caught up — nothing is waiting for review."
      : query
        ? "No requests match your search."
        : filter === "all"
          ? "No requests yet."
          : `No ${filter} requests yet.`;

  return (
    <ViaProvider>
      <div className={styles.page}>
        <header className={styles.hero}>
          <div className={styles.heroTop}>
            <button type="button" className={styles.backLink} onClick={handleBackToCustomer}>
              <span aria-hidden="true">←</span> Back to customer view
            </button>
            <div className={styles.reviewerChip}>
              <Avatar initials={initialsOf(reviewerName)} size={34} alt="" />
              <div>
                <div className={styles.reviewerName}>{reviewerName}</div>
                <div className={styles.reviewerRole}>{reviewerRole}</div>
              </div>
            </div>
          </div>

          <div className={styles.heroTitleRow}>
            <div>
              <h1 className={styles.pageTitle}>Approvals desk</h1>
              <p className={styles.pageSubtitle}>
                Requests the assistant flagged for manual review before finishing them.
              </p>
            </div>
            {counts.pending > 0 && oldestPending && (
              <div className={styles.urgent}>
                <span className={styles.urgentDot} aria-hidden="true" />
                {counts.pending} waiting · oldest {timeAgo(oldestPending)}
              </div>
            )}
          </div>

          <div className={styles.kpis}>
            <KpiTile
              label="Awaiting review"
              value={counts.pending}
              tone={counts.pending > 0 ? "attention" : undefined}
              hint={counts.pending > 0 ? "needs a decision" : "all clear"}
            />
            <KpiTile label="Approved" value={counts.approved} />
            <KpiTile label="Denied" value={counts.denied} />
            <KpiTile
              label="Approval rate"
              value={decided > 0 ? `${Math.round((counts.approved / decided) * 100)}%` : "—"}
              hint={decided > 0 ? `of ${decided} decided` : "no decisions yet"}
            />
            <KpiTile
              label="Loans approved"
              value={
                approvedLoanValue > 0
                  ? `${approvedLoanValue.toLocaleString(undefined, { maximumFractionDigits: 0 })} ${loanCurrency}`.trim()
                  : "—"
              }
              hint={undelivered > 0 ? `${undelivered} not yet delivered` : "all delivered"}
              tone={undelivered > 0 ? "warn" : undefined}
            />
          </div>
        </header>

        {notice && (
          <div className={styles.noticeWrap}>
            <Callout variant={notice.kind === "success" ? "tip" : "warning"} className={styles.notice}>
              <span role="status">{notice.text}</span>
              <button type="button" className={styles.noticeClose} onClick={() => setNotice(null)} aria-label="Dismiss">
                ×
              </button>
            </Callout>
          </div>
        )}

        <div className={styles.workspace}>
          <aside className={styles.queue} aria-label="Requests">
            <SegmentedControl
              aria-label="Filter requests"
              value={filter}
              onChange={(value) => {
                setFilter(String(value));
                setSelectedKey(null);
              }}
              className={styles.filters}
            >
              {FILTERS.map((f) => (
                <SegmentedControlItem key={f.id} value={f.id}>
                  {f.label} {counts[f.id]}
                </SegmentedControlItem>
              ))}
            </SegmentedControl>

            <input
              type="search"
              className={styles.search}
              placeholder="Search customer, loan or quote…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search requests"
            />

            {historyError && filter !== "pending" && (
              <Callout variant="warning" className={styles.queueNote}>
                {historyError}
              </Callout>
            )}

            <div className={styles.queueList}>
              {!loaded && (
                <>
                  <div className={styles.skeleton} />
                  <div className={styles.skeleton} />
                  <div className={styles.skeleton} />
                </>
              )}
              {loaded && visible.length === 0 && !(historyError && filter !== "pending") && (
                <div className={styles.empty}>
                  <div className={styles.emptyMark} aria-hidden="true">
                    ✓
                  </div>
                  <p>{emptyText}</p>
                </div>
              )}
              {visible.map((item) => (
                <QueueItem
                  key={item.key}
                  item={item}
                  isSelected={selected?.key === item.key}
                  onSelect={() => setSelectedKey(item.key)}
                />
              ))}
            </div>
          </aside>

          <section className={styles.detailPane} aria-live="polite">
            {selected ? (
              <DetailPanel
                key={selected.key}
                item={selected}
                notes={notesById[selected.review?.executionId] || ""}
                onNotes={(value) =>
                  selected.review && setNotesById((prev) => ({ ...prev, [selected.review.executionId]: value }))
                }
                isResolving={resolvingId === selected.review?.executionId}
                error={resolvedById[selected.review?.executionId]?.error}
                onDecide={handleDecision}
              />
            ) : (
              <div className={styles.detailEmpty}>
                <div className={styles.emptyMark} aria-hidden="true">
                  ✓
                </div>
                <h2>{loaded ? "Nothing selected" : "Loading requests…"}</h2>
                <p>{loaded ? "Pick a request from the queue to see the full picture." : "Fetching the review queue."}</p>
              </div>
            )}
          </section>
        </div>
      </div>
    </ViaProvider>
  );
}
