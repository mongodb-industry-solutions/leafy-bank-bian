"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from "react";
import { coreApi } from "@/lib/api/client";
import { USER_MAP } from "@/lib/constants";

const UserContext = createContext(null);

// True once the app runtime has mounted. Lives at module scope (not on
// `window`) so it survives client-side navigation but resets on any full page
// load — a refresh, a new tab, or an in-app `window.location.href` navigation.
let runtimeMounted = false;
// Breadcrumb set immediately before an intentional in-app full-page navigation
// (e.g. jumping to a backoffice route, or switching back from one). Its presence
// on the next full load distinguishes that navigation from a genuine refresh.
const INTENTIONAL_NAV_KEY = "leafy_intentional_nav";

export function UserProvider({ children }) {
  const [selectedUser, setSelectedUser] = useState(null);
  // True from the moment a login flow calls selectUser() to start prefetching data
  // until it visually finishes (modal closed / overlay dismissed). selectedUser is
  // set early (deliberately) so the destination page can start fetching in the
  // background; anything that renders a "you are logged in as X" UI (NavBar) should
  // gate on `!loginInProgress` too, or it flashes that state while the login modal
  // is still on screen.
  const [loginInProgress, setLoginInProgress] = useState(false);
  // Multi-bank: Map<consentId, { status, institution }>
  const [consents, setConsents] = useState(new Map());
  const [consentRefreshKey, setConsentRefreshKey] = useState(0);
  // Bumped to force core data (accounts/transactions) re-fetch, e.g. after a payment.
  const [dataRefreshKey, setDataRefreshKey] = useState(0);

  // Chat state — persists across navigation (e.g. bank-login redirect and back)
  const [chatMessages, setChatMessages] = useState(null); // null = fresh session, [] = cleared
  const [chatThreadId, setChatThreadId] = useState(null);

  // Hydrate from localStorage on mount (needed for bank-login tab)
  useEffect(() => {
    const stored = localStorage.getItem("selectedUser");
    if (stored) {
      try {
        const parsed = JSON.parse(stored);
        // Backfill bankUsername for sessions persisted before this field existed,
        // so openfinance calls send the DB username, not the display name.
        if (parsed && !parsed.bankUsername) {
          const details = USER_MAP[parsed.id];
          parsed.bankUsername = details?.BankUserName ?? details?.UserName ?? parsed.name;
        }
        setSelectedUser(parsed);
      } catch {
        localStorage.removeItem("selectedUser");
      }
    }
  }, []);

  const selectUser = useCallback((user) => {
    // Clear previous session
    setConsents(new Map());
    setChatMessages(null);
    setChatThreadId(null);

    // Set new user
    setSelectedUser(user);
    localStorage.setItem("selectedUser", JSON.stringify(user));
  }, []);

  const clearUser = useCallback(() => {
    localStorage.removeItem("selectedUser");
    setSelectedUser(null);
    setConsents(new Map());
  }, []);

  // Record that the next full page load is an intentional in-app navigation, so
  // the destination isn't misread as a fresh browser load that ends the session.
  // Call before any `window.location.href`/`router.push` that may cross a route
  // reached via a full page load (backoffice personas navigate this way).
  const markIntentionalNavigation = useCallback(() => {
    try {
      sessionStorage.setItem(INTENTIONAL_NAV_KEY, "1");
    } catch {
      /* sessionStorage unavailable (SSR/private mode) — fall back to clearing */
    }
  }, []);

  // Decide, once per runtime, whether this mount is a genuine fresh browser load
  // (refresh / new tab) versus a continuation of an in-app session. Returns false
  // for client-side navigations (runtime already mounted) and for intentional
  // full-page navigations (breadcrumb present, consumed here).
  const isFreshBrowserLoad = useCallback(() => {
    if (runtimeMounted) return false;
    runtimeMounted = true;
    let intentional = false;
    try {
      intentional = sessionStorage.getItem(INTENTIONAL_NAV_KEY) === "1";
      sessionStorage.removeItem(INTENTIONAL_NAV_KEY);
    } catch {
      /* treat as fresh load if sessionStorage is unavailable */
    }
    return !intentional;
  }, []);

  // Update bearer token (e.g. from bank-login get-authorization)
  const updateBearerToken = useCallback((token) => {
    setSelectedUser((prev) => {
      if (!prev) return prev;
      const updated = { ...prev, bearerToken: token };
      localStorage.setItem("selectedUser", JSON.stringify(updated));
      return updated;
    });
  }, []);

  // Force accounts/transactions hooks to re-fetch (e.g. after a payment settles).
  const refreshData = useCallback(() => setDataRefreshKey((k) => k + 1), []);

  // Multi-bank: add a consent (appends, doesn't overwrite)
  const addConsent = useCallback((consentId, status, institution) => {
    setConsents((prev) => {
      const next = new Map(prev);
      next.set(consentId, { status, institution });
      return next;
    });
    setConsentRefreshKey((k) => k + 1);
  }, []);

  // Multi-bank: remove a specific consent (revocation)
  const removeConsent = useCallback((consentId) => {
    setConsents((prev) => {
      const next = new Map(prev);
      next.delete(consentId);
      return next;
    });
    setConsentRefreshKey((k) => k + 1);
  }, []);

  // Listen for consent completion broadcast from the bank-login tab.
  // useBankLogin posts { type: "consent_complete", consentId, institution, bearerToken }
  // on the "leafy-bank-consent" channel. Register the consent, store the token,
  // then prime the backend cache so the dashboard can read it once per user.
  useEffect(() => {
    const channel = new BroadcastChannel("leafy-bank-consent");

    channel.onmessage = (event) => {
      const msg = event.data;
      if (msg?.type !== "consent_complete" || !msg.consentId) return;

      if (msg.bearerToken) updateBearerToken(msg.bearerToken);
      addConsent(msg.consentId, "authorized", msg.institution);

      // Prime the cache (fetch-and-cache) so useCachedExternalData has data to read.
      const userName = selectedUser?.bankUsername;
      if (userName && msg.bearerToken) {
        coreApi(
          `openfinance/secure/customers/${userName}/fetch-and-cache`,
          {
            method: "POST",
            bearerToken: msg.bearerToken,
            params: { consent_id: msg.consentId },
          },
        ).then(() => {
          // Re-read the cache once population completes.
          setConsentRefreshKey((k) => k + 1);
        });
      }
    };

    return () => channel.close();
  }, [selectedUser?.name, addConsent, updateBearerToken]);

  // Reconcile local consents against backend status: the ConsentSweeper expires
  // consents server-side (and purges their cached data) with no push to the client,
  // so poll and drop any we track that are no longer AUTHORISED. Empties
  // authorizedConsents on expiry → "Connected to X" badges + Global Position vanish
  // on their own (both derive from authorizedConsents).
  useEffect(() => {
    if (!selectedUser?.name || !selectedUser?.bearerToken || consents.size === 0) return;

    let cancelled = false;
    const reconcile = async () => {
      // Trailing slash matches the router's `@router.get("/")` — without it
      // FastAPI 307-redirects to the slash form (extra round-trip, and some
      // proxies drop the Authorization header on the redirect hop).
      const { data, error } = await coreApi(
        `openfinance/secure/consents/`,
        { bearerToken: selectedUser.bearerToken, params: { consumer_id: selectedUser.bankUsername } },
      );
      if (cancelled || error || !data?.consents) return;

      const stillActive = new Set(
        data.consents
          .filter((c) => c.Status === "AUTHORISED")
          .map((c) => c.ConsentId),
      );
      for (const consentId of consents.keys()) {
        if (!stillActive.has(consentId)) removeConsent(consentId);
      }
    };

    reconcile(); // immediate check on mount / dependency change
    const id = setInterval(reconcile, 30000); // and every 30s
    const onFocus = () => reconcile();
    window.addEventListener("focus", onFocus);

    return () => {
      cancelled = true;
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [selectedUser?.name, selectedUser?.bearerToken, consents, removeConsent]);

  // Derived: all authorized consents as array
  const authorizedConsents = useMemo(
    () =>
      [...consents.entries()]
        .filter(([, c]) => c.status === "authorized")
        .map(([id, c]) => ({ consentId: id, ...c })),
    [consents]
  );

  const authorizedConsentIds = useMemo(
    () => authorizedConsents.map((c) => c.consentId),
    [authorizedConsents]
  );

  const hasActiveConsent = authorizedConsents.length > 0;

  // Backward compat bridge (deprecated — use authorizedConsents instead)
  const activeConsentId = authorizedConsents[0]?.consentId ?? null;
  const consentStatus = authorizedConsents[0]?.status ?? null;
  const sourceInstitution = authorizedConsents[0]?.institution ?? null;
  const setConsent = addConsent;

  const value = {
    selectedUser,
    selectUser,
    clearUser,
    loginInProgress,
    setLoginInProgress,
    markIntentionalNavigation,
    isFreshBrowserLoad,
    updateBearerToken,
    // Multi-bank API
    consents,
    addConsent,
    removeConsent,
    authorizedConsents,
    authorizedConsentIds,
    hasActiveConsent,
    // Backward compat (deprecated)
    activeConsentId,
    consentStatus,
    sourceInstitution,
    setConsent,
    consentRefreshKey,
    dataRefreshKey,
    refreshData,
    chatMessages,
    setChatMessages,
    chatThreadId,
    setChatThreadId,
  };

  return <UserContext.Provider value={value}>{children}</UserContext.Provider>;
}

export function useUser() {
  const ctx = useContext(UserContext);
  if (!ctx) throw new Error("useUser must be used within UserProvider");
  return ctx;
}
