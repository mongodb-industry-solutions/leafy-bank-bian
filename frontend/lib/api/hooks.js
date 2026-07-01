"use client";

import { useUser } from "@/lib/context/UserContext";
import { useEffect, useState } from "react";
import { coreApi, pipelineApi } from "./client";

// BIAN backend field names differ from what composed hooks expect.
// These helpers normalize the wire shape once so composed hooks need no changes.

const ACCOUNT_TYPE_MAP = {
  CURRENT: "Checking",
  SAVINGS: "Savings",
  FIXED_DEPOSIT: "FixedDeposit",
};

function normalizeAccount(a) {
  return {
    ...a,
    AccountBalance: a.balance?.current ?? 0,
    AccountType: ACCOUNT_TYPE_MAP[a.type] ?? a.type,
    AccountNumber: a.accountNumber,
    AccountBank: a.accountBank,
    AccountDescription: a.description ?? "",
  };
}

function normalizeTransaction(t) {
  return {
    ...t,
    BookgDt: t.bookingDate,
    Amt: { value: t.amount },
    CdtDbtInd: t.direction === "OUTGOING" ? "DBIT" : "CRDT",
    Cdtr: { Nm: t.payee?.name },
    AddtlNtryInf: t.description,
    BkTxCd: { Fmly: t.txnCode?.split("-")[0] ?? "PMNT" },
  };
}

/**
 * Fetch internal Leafy Bank accounts for the logged-in user.
 * Calls BIAN CurrentAccountFulfillmentArrangement/Request.
 */
export function useAccounts() {
  const { selectedUser, dataRefreshKey } = useUser();
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!selectedUser?.id) {
      setLoading(false);
      return;
    }
    setLoading(true);

    const customerId = `CUST-${selectedUser.id.slice(-8)}`;
    coreApi("CurrentAccountFulfillmentArrangement/Request", {
      method: "POST",
      body: { customerId },
    }).then(({ data, error: err }) => {
      if (data?.accounts) setAccounts(data.accounts.map(normalizeAccount));
      if (err) setError(err);
      setLoading(false);
    });
  }, [selectedUser?.id, dataRefreshKey]);

  return { accounts, loading, error };
}

/**
 * Fetch every CURRENT/SAVINGS account bank-wide (any customer), each tagged
 * with its owner's legal name, so a beneficiary picker can be a plain
 * dropdown instead of a type-and-lookup field. GL/NOSTRO/VOSTRO accounts are
 * excluded — those are internal ledger accounts, not payment counterparties.
 * Calls BIAN CurrentAccountFulfillmentArrangement/Request + PartyReferenceDataDirectoryEntry/Request.
 */
export function useBeneficiaryAccounts() {
  const [beneficiaryAccounts, setBeneficiaryAccounts] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      coreApi("CurrentAccountFulfillmentArrangement/Request", { method: "POST", body: {} }),
      coreApi("PartyReferenceDataDirectoryEntry/Request", { method: "POST", body: {} }),
    ]).then(([accountsRes, customersRes]) => {
      const legalNameByCustomerId = new Map(
        (customersRes.data?.customers ?? []).map((c) => [c.customerId, c.identification?.legalName]),
      );
      const options = (accountsRes.data?.accounts ?? [])
        .filter((a) => a.type === "CURRENT" || a.type === "SAVINGS")
        .map((a) => ({
          id: a.accountId,
          label: `${a.type} - ${a.accountNumber}`,
          ownerName: legalNameByCustomerId.get(a.customerSnapshot?.customerId) ?? null,
          currency: a.currency,
        }));
      setBeneficiaryAccounts(options);
      setLoading(false);
    });
  }, []);

  return { beneficiaryAccounts, loading };
}

/**
 * Fetch all transactions for the logged-in user.
 * Calls BIAN CurrentAccountFulfillmentArrangement/CurrentAccountTransaction/Request.
 */
export function useTransactions() {
  const { selectedUser, dataRefreshKey } = useUser();
  const [transactions, setTransactions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!selectedUser?.id) {
      setLoading(false);
      return;
    }
    setLoading(true);

    const customerId = `CUST-${selectedUser.id.slice(-8)}`;
    coreApi("CurrentAccountFulfillmentArrangement/CurrentAccountTransaction/Request", {
      method: "POST",
      body: { customerId, limit: 50 },
    }).then(({ data, error: err }) => {
      if (data?.transactions) setTransactions(data.transactions.map(normalizeTransaction));
      if (err) setError(err);
      setLoading(false);
    });
  }, [selectedUser?.id, dataRefreshKey]);

  return { transactions, loading, error };
}

/**
 * Fetch credit risk rating for the logged-in user via BIAN KYC record.
 * Calls BIAN PartyReferenceDataDirectoryEntry/CustomerKYCRecord/Retrieve.
 */
export function useCreditScore() {
  const { selectedUser } = useUser();
  const [creditScore, setCreditScore] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!selectedUser?.id) {
      setLoading(false);
      return;
    }
    setLoading(true);

    const customerId = `CUST-${selectedUser.id.slice(-8)}`;
    coreApi("PartyReferenceDataDirectoryEntry/CustomerKYCRecord/Retrieve", {
      method: "POST",
      body: { customerId },
    }).then(({ data }) => {
      if (data?.kyc?.riskRating) setCreditScore(data.kyc.riskRating);
      setLoading(false);
    });
  }, [selectedUser?.id]);

  return { creditScore, loading };
}

/**
 * Fetch external accounts from ALL consented institutions (multi-bank).
 * Fires parallel fetches per authorized consent, merges results.
 * Each account is tagged with _sourceInstitution and _consentId.
 */
export function useExternalAccounts() {
  const { selectedUser, authorizedConsents, consentRefreshKey, removeConsent } =
    useUser();
  const [externalAccounts, setExternalAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (
      !selectedUser?.name ||
      !selectedUser?.bearerToken ||
      authorizedConsents.length === 0
    ) {
      setExternalAccounts([]);
      return;
    }
    setLoading(true);

    const fetchAll = async () => {
      try {
        const results = await Promise.all(
          authorizedConsents.map(async ({ consentId, institution }) => {
            const { data, error: err } = await coreApi(
              "openfinance/secure/fetch-external-accounts-for-user/",
              {
                bearerToken: selectedUser.bearerToken,
                params: {
                  user_identifier: selectedUser.name,
                  consent_id: consentId,
                },
              },
            );
            if (err) {
              // 403 means consent is stale/revoked — remove it from context
              if (err.startsWith("403")) {
                removeConsent(consentId);
              }
              return [];
            }
            return (data?.accounts || []).map((a) => ({
              ...a,
              _sourceInstitution: institution,
              _consentId: consentId,
            }));
          }),
        );
        setExternalAccounts(results.flat());
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };

    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedUser?.name,
    selectedUser?.bearerToken,
    authorizedConsents,
    consentRefreshKey,
  ]);

  return { externalAccounts, loading, error };
}

/**
 * Fetch external products/loans from ALL consented institutions (multi-bank).
 * Fires parallel fetches per authorized consent, merges results.
 * Each product is tagged with _sourceInstitution and _consentId.
 */
export function useExternalProducts() {
  const { selectedUser, authorizedConsents, consentRefreshKey, removeConsent } =
    useUser();
  const [externalProducts, setExternalProducts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (
      !selectedUser?.name ||
      !selectedUser?.bearerToken ||
      authorizedConsents.length === 0
    ) {
      setExternalProducts([]);
      return;
    }
    setLoading(true);

    const fetchAll = async () => {
      try {
        const results = await Promise.all(
          authorizedConsents.map(async ({ consentId, institution }) => {
            const { data, error: err } = await coreApi(
              "openfinance/secure/fetch-external-products-for-user/",
              {
                bearerToken: selectedUser.bearerToken,
                params: {
                  user_identifier: selectedUser.name,
                  consent_id: consentId,
                },
              },
            );
            if (err) {
              // 403 means consent is stale/revoked — remove it from context
              if (err.startsWith("403")) {
                removeConsent(consentId);
              }
              return [];
            }
            return (data?.products || []).map((p) => ({
              ...p,
              _sourceInstitution: institution,
              _consentId: consentId,
            }));
          }),
        );
        setExternalProducts(results.flat());
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };

    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedUser?.name,
    selectedUser?.bearerToken,
    authorizedConsents,
    consentRefreshKey,
  ]);

  return { externalProducts, loading, error };
}

/**
 * Fetch external transactions from ALL consented institutions (multi-bank).
 * Fires parallel fetches per authorized consent, merges results.
 * Each transaction is tagged with _sourceInstitution and _consentId.
 */
export function useExternalTransactions() {
  const {
    selectedUser,
    authorizedConsents,
    consentRefreshKey,
    removeConsent,
    profile,
  } = useUser();
  const [externalTransactions, setExternalTransactions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (
      !selectedUser?.name ||
      !selectedUser?.bearerToken ||
      authorizedConsents.length === 0
    ) {
      setExternalTransactions([]);
      return;
    }
    setLoading(true);

    const fetchAll = async () => {
      try {
        const results = await Promise.all(
          authorizedConsents.map(async ({ consentId, institution }) => {
            const params = {
              consent_id: consentId,
            };
            if (profile) params.profile = profile;

            const { data, error: err } = await coreApi(
              `openfinance/secure/customers/${selectedUser.name}/external-transactions`,
              {
                bearerToken: selectedUser.bearerToken,
                params,
              },
            );
            if (err) {
              if (err.startsWith("403")) {
                removeConsent(consentId);
              }
              return [];
            }
            return (data?.transactions || []).map((t) => ({
              ...t,
              _sourceInstitution:
                institution || data?.source_institution || "External",
              _consentId: consentId,
            }));
          }),
        );
        setExternalTransactions(results.flat());
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };

    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedUser?.name,
    selectedUser?.bearerToken,
    authorizedConsents,
    consentRefreshKey,
    profile,
  ]);

  return { externalTransactions, loading, error };
}

// ────────────────────────────────────────────────────────────
// Composed Hooks — one per page, returns page-ready data
// These call raw hooks internally. Pages import ONE composed hook.
// See .claude/memory/coding-patterns.md for the full pattern.
// ────────────────────────────────────────────────────────────

import { formatDate, txCategory } from "./format";

/**
 * Composed hook for the Home page.
 * Aggregates accounts, credit score, and external products into dashboard-ready data.
 */
export function useHomeData() {
  const { accounts, loading: accountsLoading } = useAccounts();
  const { creditScore } = useCreditScore();
  const { externalProducts } = useExternalProducts();

  const totalBalance = accounts
    .filter((a) => a.AccountBalance > 0)
    .reduce((sum, a) => sum + a.AccountBalance, 0);

  const internalDebt = accounts
    .filter((a) => a.AccountBalance < 0)
    .reduce((sum, a) => sum + Math.abs(a.AccountBalance), 0);

  const externalDebt = externalProducts.reduce(
    (sum, p) => sum + (p.ProductBalance || 0),
    0,
  );

  const bankAccounts = accounts.filter(
    (a) => a.AccountType === "Checking" || a.AccountType === "Savings",
  );

  const creditCards = accounts.filter((a) => a.AccountType === "CreditCard");

  const loans = externalProducts.map((p) => ({
    name: p.ProductName || p.ProductType || "Loan",
    balance: p.ProductBalance || 0,
    institution: p._sourceInstitution || p.ProductBank || "",
  }));

  return {
    totalBalance,
    totalDebt: internalDebt + externalDebt,
    bankAccounts,
    creditCards,
    creditScore,
    loans,
    loading: accountsLoading,
  };
}

/**
 * Composed hook for the Accounts page.
 * Merges internal + external accounts for OverlapCards,
 * and merges internal + external transactions for the table.
 */
export function useAccountsPageData() {
  const { accounts, loading: accountsLoading } = useAccounts();
  const { transactions, loading: txLoading } = useTransactions();
  const { externalAccounts } = useExternalAccounts();
  const { externalTransactions, loading: extTxLoading } =
    useExternalTransactions();

  const bankAccounts = accounts
    .filter((a) => a.AccountType === "Checking" || a.AccountType === "Savings")
    .map((a) => ({
      title: `${a.AccountType} Account`,
      number: a.AccountNumber,
      amount: a.AccountBalance,
      bank: a.AccountBank,
    }));

  const extCards = externalAccounts.map((a) => ({
    title: `${a.AccountType || "External"} (${a._sourceInstitution || "External"})`,
    number: a.AccountNumber || a.account_number || "",
    amount: a.AccountBalance || a.account_balance || 0,
    bank: a.AccountBank || a._sourceInstitution || "",
  }));

  const allAccounts = [...bankAccounts, ...extCards];

  const internalTxns = transactions.map((t) => ({
    category: txCategory(t),
    establishment: t.Cdtr?.Nm || t.AddtlNtryInf || "\u2014",
    date: formatDate(t.BookgDt),
    amount: t.Amt?.value || 0,
    type: t.CdtDbtInd === "CRDT" ? "Credit" : "Debit",
    paymentId: t.paymentId,
    _isExternal: false,
    _rawDate: t.BookgDt || "",
    _rawDocument: t,
  }));

  const externalTxns = externalTransactions.map((t) => ({
    category: txCategory(t),
    establishment: t.Cdtr?.Nm || t.AddtlNtryInf || "\u2014",
    date: formatDate(t.BookgDt),
    amount: t.Amt?.value || 0,
    type: t.CdtDbtInd === "CRDT" ? "Credit" : "Debit",
    _isExternal: true,
    _sourceInstitution: t._sourceInstitution,
    _rawDate: t.BookgDt || "",
    _rawDocument: t,
  }));

  const recentTxns = [...internalTxns, ...externalTxns]
    .sort((a, b) => new Date(b._rawDate) - new Date(a._rawDate))
    .slice(0, 20);

  return {
    allAccounts,
    recentTxns,
    accountsLoading,
    txLoading: txLoading || extTxLoading,
  };
}

/**
 * Composed hook for the Credit Cards page.
 * Merges internal + external credit cards for OverlapCards,
 * and merges internal + external card transactions for the table.
 */
export function useCreditCardsPageData() {
  const { accounts, loading: accountsLoading } = useAccounts();
  const { transactions, loading: txLoading } = useTransactions();
  const { externalAccounts } = useExternalAccounts();
  const { externalTransactions, loading: extTxLoading } =
    useExternalTransactions();

  const internalCards = accounts
    .filter((a) => a.AccountType === "CreditCard")
    .map((a) => ({
      title: a.AccountDescription || "Credit Card",
      number: a.AccountNumber,
      amount: Math.abs(a.AccountBalance),
      bank: a.AccountBank,
    }));

  const externalCards = externalAccounts
    .filter(
      (a) =>
        (a.AccountType || "").toUpperCase() === "CREDITCARD" ||
        (a.Acct?.Tp || "") === "CARD",
    )
    .map((a) => ({
      title: `Credit Card (${a._sourceInstitution || "External"})`,
      number: a.AccountNumber || a.Acct?.Id || "",
      amount: Math.abs(a.AccountBalance || a.account_balance || 0),
      bank: a.AccountBank || a._sourceInstitution || "",
    }));

  const creditCards = [...internalCards, ...externalCards];

  const internalCardTxns = transactions
    .filter((t) => t.Acct?.Tp === "CARD")
    .map((t) => ({
      category: txCategory(t),
      establishment: t.Cdtr?.Nm || t.AddtlNtryInf || "\u2014",
      date: formatDate(t.BookgDt),
      amount: t.Amt?.value || 0,
      _isExternal: false,
      _rawDate: t.BookgDt || "",
      _rawDocument: t,
    }));

  // External card transactions: filter by MCRD (merchant card) family code
  const externalCardTxns = externalTransactions
    .filter((t) => t.BkTxCd?.Fmly === "MCRD")
    .map((t) => ({
      category: txCategory(t),
      establishment: t.Cdtr?.Nm || t.AddtlNtryInf || "\u2014",
      date: formatDate(t.BookgDt),
      amount: t.Amt?.value || 0,
      _isExternal: true,
      _sourceInstitution: t._sourceInstitution,
      _rawDate: t.BookgDt || "",
      _rawDocument: t,
    }));

  const cardTxns = [...internalCardTxns, ...externalCardTxns]
    .sort((a, b) => new Date(b._rawDate) - new Date(a._rawDate))
    .slice(0, 20);

  return {
    creditCards,
    cardTxns,
    accountsLoading,
    txLoading: txLoading || extTxLoading,
  };
}

/**
 * Composed hook for the Loans page.
 * Transforms external products into OverlapCards format and normalized table rows.
 */
export function useLoansPageData() {
  const { hasActiveConsent } = useUser();
  const { externalProducts, loading } = useExternalProducts();

  const loanCards = externalProducts.map((p) => ({
    title: p.ProductName || p.ProductType || "Loan",
    number: p.ProductId || p._id || "",
    amount: p.ProductBalance || 0,
    originalAmount: p.ProductAmount || 0,
    bank: p._sourceInstitution || p.ProductBank || "",
  }));

  const loanTableRows = externalProducts.map((p) => ({
    type: p.LoanSubType || p.ProductType || "Loan",
    institution: p._sourceInstitution || p.ProductBank || "\u2014",
    contract: p.ProductId || "\u2014",
    outstanding: p.ProductBalance || 0,
    originalAmount: p.ProductAmount || 0,
  }));

  return { loanCards, loanTableRows, loading, hasActiveConsent };
}

/**
 * Live trace of one payment through the GL pipeline.
 * Polls /pipeline/trace/{paymentId} every `intervalMs` while `enabled` and the
 * pipeline is incomplete (journal not yet posted). Stops once the journal entry
 * lands, on a 404, or when disabled.
 */
export function usePipelineTrace(paymentId, enabled, intervalMs = 2000) {
  const [trace, setTrace] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!enabled || !paymentId) {
      setTrace(null);
      setError(null);
      return;
    }

    let cancelled = false;
    let timer = null;

    const poll = async () => {
      const { data, error: err } = await pipelineApi(`trace/${paymentId}`);
      if (cancelled) return;

      if (err) {
        setError(err);
        setLoading(false);
        return; // stop polling on error (incl. 404)
      }

      setTrace(data);
      setLoading(false);

      // Keep polling until the journal entry is posted.
      if (!data?.journalEntry) {
        timer = setTimeout(poll, intervalMs);
      }
    };

    setLoading(true);
    setError(null);
    poll();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [paymentId, enabled, intervalMs]);

  return { trace, loading, error };
}

/**
 * Pipeline health snapshot — used for the next-GL-batch countdown.
 * Fetched once when `enabled` flips true.
 */
export function usePipelineHealth(enabled) {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    pipelineApi("health").then(({ data }) => {
      if (!cancelled && data) setHealth(data);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return health;
}
