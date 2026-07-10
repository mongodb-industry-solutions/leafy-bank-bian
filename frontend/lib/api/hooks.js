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
    // BIAN txnCode is "PMNT-MCRD-POSD": family is the SECOND segment (MCRD=card),
    // subfamily the third (POSD=point-of-sale). The card view filters on Fmly === "MCRD".
    BkTxCd: {
      Fmly: t.txnCode?.split("-")[1] ?? "PMNT",
      SubFmly: t.txnCode?.split("-")[2],
    },
  };
}

// All identifiers a transaction may use to reference its owning account.
// Internal txns key on accountId; external ones on payer/payee.accountNo. Both
// shapes (and BIAN payer/payee.accountId) are collected so a selected account
// can be matched regardless of source.
function txnAccountKeys(t) {
  return [
    t.accountId,
    t.payer?.accountId,
    t.payee?.accountId,
    t.payer?.accountNo,
    t.payee?.accountNo,
    t.payer?.accountNumber,
    t.payee?.accountNumber,
  ].filter(Boolean);
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
 * Fetch ALL cached external data (accounts, products, transactions) for the
 * user in one round trip via the backend caching layer.
 *
 * The dashboard uses fetch-once-then-read-cache: on consent approval the
 * consent listener calls fetch-and-cache; this hook then reads the durable
 * cache, so the dashboard survives one-time-consent consumption and the
 * sweeper-driven expiry model. Called ONCE per user, not per-consent.
 *
 * The cache returns raw BIAN source docs grouped by institution:
 *   institutions[] -> { institution, consent_id, accounts[], products[], transactions[] }
 * Accounts/products are already PascalCase (AccountBalance, ProductName, ...).
 * Transactions are BIAN-shaped, so they run through normalizeTransaction here.
 * Every row is tagged with _sourceInstitution and _consentId for per-bank badges.
 *
 * A partial payload (e.g. accounts + transactions but no products, when a
 * consent lacks LOANS_READ) is normal — it never clears the consent.
 */
export function useCachedExternalData() {
  const { selectedUser, authorizedConsents, authorizedConsentIds, consentRefreshKey } = useUser();
  const [externalAccounts, setExternalAccounts] = useState([]);
  const [externalProducts, setExternalProducts] = useState([]);
  const [externalTransactions, setExternalTransactions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (
      !selectedUser?.name ||
      !selectedUser?.bearerToken ||
      authorizedConsents.length === 0
    ) {
      setExternalAccounts([]);
      setExternalProducts([]);
      setExternalTransactions([]);
      return;
    }
    setLoading(true);
    let cancelled = false;

    coreApi(
      `openfinance/secure/customers/${selectedUser.name}/cached-data`,
      {
        bearerToken: selectedUser.bearerToken,
        // Scope to THIS browser session's consents so cross-session/duplicate
        // cached data from other sessions never appears on the dashboard.
        params: { consent_ids: authorizedConsentIds.join(",") },
      },
    ).then(({ data, error: err }) => {
      if (cancelled) return;
      if (err || !data?.institutions) {
        if (err) setError(err);
        setExternalAccounts([]);
        setExternalProducts([]);
        setExternalTransactions([]);
        setLoading(false);
        return;
      }

      const accounts = [];
      const products = [];
      const transactions = [];
      for (const inst of data.institutions) {
        const tag = {
          _sourceInstitution: inst.institution || "External",
          _consentId: inst.consent_id,
        };
        for (const a of inst.accounts || []) accounts.push({ ...a, ...tag });
        for (const p of inst.products || []) products.push({ ...p, ...tag });
        for (const t of inst.transactions || [])
          transactions.push({ ...normalizeTransaction(t), ...tag });
      }

      setExternalAccounts(accounts);
      setExternalProducts(products);
      setExternalTransactions(transactions);
      setError(null);
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedUser?.name,
    selectedUser?.bearerToken,
    authorizedConsents,
    consentRefreshKey,
  ]);

  return { externalAccounts, externalProducts, externalTransactions, loading, error };
}

/**
 * Server-computed global position: folds internal Leafy Bank balances with the
 * cached external data into { total_balance, total_debt, net_worth, by_institution }.
 * Requires an authorized consent and that fetch-and-cache has run (Phase 1).
 * Returns null when there is no consent — callers fall back to internal-only math.
 */
export function useGlobalPosition() {
  const { selectedUser, authorizedConsents, authorizedConsentIds, consentRefreshKey } = useUser();
  const [position, setPosition] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (
      !selectedUser?.name ||
      !selectedUser?.bearerToken ||
      authorizedConsents.length === 0
    ) {
      setPosition(null);
      return;
    }
    setLoading(true);
    let cancelled = false;

    coreApi(
      `openfinance/secure/customers/${selectedUser.name}/global-position`,
      {
        bearerToken: selectedUser.bearerToken,
        // Match the cached-data view: only this session's banks, so totals
        // never double-count duplicates from other sessions.
        params: { consent_ids: authorizedConsentIds.join(",") },
      },
    ).then(({ data, error }) => {
      if (cancelled) return;
      setPosition(error ? null : data);
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedUser?.name,
    selectedUser?.bearerToken,
    authorizedConsents,
    consentRefreshKey,
  ]);

  return { position, loading };
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
  const { externalAccounts, externalProducts } = useCachedExternalData();
  const { position } = useGlobalPosition();

  // Internal-only totals — used as the no-consent fallback and before the
  // server-side global position has loaded.
  const internalBalance = accounts
    .filter((a) => a.AccountBalance > 0)
    .reduce((sum, a) => sum + a.AccountBalance, 0);

  const internalDebt = accounts
    .filter((a) => a.AccountBalance < 0)
    .reduce((sum, a) => sum + Math.abs(a.AccountBalance), 0);

  const externalDebt = externalProducts.reduce(
    (sum, p) => sum + (p.ProductBalance || 0),
    0,
  );

  // Prefer the server-computed global position (folds internal + cached external);
  // fall back to client-side internal + external math when it is unavailable.
  const totalBalance = position?.total_balance ?? internalBalance;
  const totalDebt = position?.total_debt ?? internalDebt + externalDebt;

  // Internal Leafy Bank accounts + external accounts pulled via consent.
  // External docs are already PascalCase (AccountType/Number/Bank/Balance) and
  // carry _sourceInstitution as their bank tag, so they need no remapping —
  // just fall back to the tag for the badge when AccountBank is absent.
  const isCheckingOrSavings = (a) =>
    a.AccountType === "Checking" || a.AccountType === "Savings";

  const externalBankAccounts = externalAccounts
    .filter(isCheckingOrSavings)
    .map((a) => ({ ...a, AccountBank: a.AccountBank || a._sourceInstitution }));

  const bankAccounts = [
    ...accounts.filter(isCheckingOrSavings),
    ...externalBankAccounts,
  ];

  // Internal credit cards + external cards pulled via consent. External card
  // docs use AccountType "CreditCard" (or Acct.Tp "CARD"); they're PascalCase
  // like the accounts above, so tag the bank from _sourceInstitution when absent.
  const isCreditCard = (a) =>
    (a.AccountType || "").toUpperCase() === "CREDITCARD" ||
    (a.Acct?.Tp || "") === "CARD";

  const externalCreditCards = externalAccounts
    .filter(isCreditCard)
    .map((a) => ({ ...a, AccountBank: a.AccountBank || a._sourceInstitution }));

  const creditCards = [
    ...accounts.filter((a) => a.AccountType === "CreditCard"),
    ...externalCreditCards,
  ];

  const loans = externalProducts.map((p) => ({
    name: p.ProductName || p.ProductType || "Loan",
    balance: p.ProductBalance || 0,
    institution: p._sourceInstitution || p.ProductBank || "",
  }));

  return {
    totalBalance,
    totalDebt,
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
  const {
    externalAccounts,
    externalTransactions,
    loading: extTxLoading,
  } = useCachedExternalData();

  const bankAccounts = accounts
    .filter((a) => a.AccountType === "Checking" || a.AccountType === "Savings")
    .map((a) => ({
      id: a.accountId,
      title: `${a.AccountType} Account`,
      number: a.AccountNumber,
      amount: a.AccountBalance,
      bank: a.AccountBank,
    }));

  const extCards = externalAccounts.map((a) => ({
    id: a.accountId || null,
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
    transactionStatus: t.transactionStatus || t.status,
    _isExternal: false,
    _accountKeys: txnAccountKeys(t),
    _rawDate: t.BookgDt || "",
    _rawDocument: t,
  }));

  const externalTxns = externalTransactions.map((t) => ({
    category: txCategory(t),
    establishment: t.Cdtr?.Nm || t.AddtlNtryInf || "\u2014",
    date: formatDate(t.BookgDt),
    amount: t.Amt?.value || 0,
    type: t.CdtDbtInd === "CRDT" ? "Credit" : "Debit",
    transactionStatus: t.transactionStatus || t.status,
    _isExternal: true,
    _sourceInstitution: t._sourceInstitution,
    _accountKeys: txnAccountKeys(t),
    _rawDate: t.BookgDt || "",
    _rawDocument: t,
  }));

  const recentTxns = [...internalTxns, ...externalTxns]
    .sort((a, b) => new Date(b._rawDate) - new Date(a._rawDate))
    .slice(0, 50);

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
  const {
    externalAccounts,
    externalTransactions,
    loading: extTxLoading,
  } = useCachedExternalData();

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

  // Account numbers of the actual external credit cards. A transaction only
  // counts as a card transaction if it belongs to one of these accounts —
  // the MCRD (merchant-card) family alone also matches card purchases made
  // on a checking account, which are not credit-card transactions.
  const externalCardNumbers = new Set(
    externalCards.map((c) => c.number).filter(Boolean),
  );

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

  // External card transactions: those posted to an actual credit-card account
  // (matched by payer account number), not merely any MCRD-family transaction.
  const externalCardTxns = externalTransactions
    .filter((t) => externalCardNumbers.has(t.payer?.accountNo))
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
    .slice(0, 50);

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
  const { externalProducts, loading } = useCachedExternalData();

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
 * Polls /pipeline/health on a light cadence and returns the latest
 * `lastBatchAt`. Batch-derived views (the journal column, the GL dashboard
 * totals) use the returned value as a refetch key, so they update when the GL
 * batch *actually* posts — reacting to real completion instead of a guessed
 * timer. Returns the ISO string (its change is the "batch ran" signal) or null
 * before the first batch. Interval defaults to 30s; the batch cadence is 10 min.
 */
export function useBatchTick(enabled = true, intervalMs = 30000) {
  const [lastBatchAt, setLastBatchAt] = useState(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      const { data } = await pipelineApi("health");
      if (cancelled) return;
      if (data?.lastBatchAt) setLastBatchAt(data.lastBatchAt);
      timer = setTimeout(poll, intervalMs);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [enabled, intervalMs]);

  return lastBatchAt;
}

/**
 * GL dashboard snapshot — one call feeds all dashboard blocks (KPI tiles,
 * journal-status donut, reconciliation roll-up, top control accounts).
 * Monthly granularity. Pass a periodCode for a single month, or omit it to
 * roll up the last `months` months (default 3, including the current month).
 * Amounts are minor units (int) — divide by 100 for display.
 *
 * @param {string|null} periodCode - "YYYY-MM" for a single month, or null for the rolling window
 * @param {boolean} enabled - gate the fetch (e.g. only when the page is shown)
 * @param {number} [topN=5] - number of top control accounts to return
 * @param {number} [months=3] - window size when periodCode is null
 * @param {*} [refreshKey] - change this to force a refetch (e.g. useBatchTick())
 * @returns {{dashboard: object|null, loading: boolean, error: string|null}}
 */
export function useGlDashboard(periodCode, enabled, topN = 5, months = 3, refreshKey) {
  const [dashboard, setDashboard] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    // Only send months for the rolling-window case; a fixed periodCode ignores it.
    const params = periodCode ? { periodCode, topN } : { topN, months };
    pipelineApi("gl-dashboard", params).then(({ data, error: err }) => {
      if (cancelled) return;
      if (err) {
        setError(err);
      } else {
        setDashboard(data);
      }
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [periodCode, enabled, topN, months, refreshKey]);

  return { dashboard, loading, error };
}
