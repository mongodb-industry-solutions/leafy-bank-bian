"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from "react";

const UserContext = createContext(null);

export function UserProvider({ children }) {
  const [selectedUser, setSelectedUser] = useState(null);
  // Multi-bank: Map<consentId, { status, institution }>
  const [consents, setConsents] = useState(new Map());
  const [profile, setProfile] = useState("balanced");
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
        setSelectedUser(JSON.parse(stored));
      } catch {
        localStorage.removeItem("selectedUser");
      }
    }
  }, []);

  const selectUser = useCallback((user) => {
    // Clear previous session
    setConsents(new Map());
    setProfile("balanced");
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
    setProfile("balanced");
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
    profile,
    setProfile,
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
