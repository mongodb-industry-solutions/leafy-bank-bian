/**
 * Headless "connect your accounts" flow against the real open-finance-next-gen
 * backend (proxied via CONSENT_BACKEND_URL — see app/api/backend/[...path]/
 * route.js). No chat/LLM involved on either side: this is plain sequential
 * API calls (get-authorization -> create consent -> approve -> fetch-and-cache),
 * driven directly by the customer's own "yes, connect" click in the UI.
 *
 * Agent Engine never calls any of this. It doesn't need to be told about it
 * either: fetch-and-cache persists into `cachedExternalData` in
 * leafy_bank_bian_magenta — the same database/cluster Agent Engine's
 * financial_wellness domain already reads directly (see
 * insights_service.py) — so Agent Engine just picks it up next time it computes,
 * the same read-only pattern already used for loans/portfolioAllocation.
 * No separate write-back route needed on our side at all.
 *
 * `expiration_days` is treated as MINUTES by this backend in demo mode
 * (confirmed against a live call) — approve/fetch-and-cache must happen
 * promptly after create, which this module already does back-to-back.
 * Only DURATION_BASED consents (expiration_days > 0) can be cached this way.
 *
 * Institutions with real seeded account/product data today: "Green Bank",
 * "MongoDB Bank" (confirmed against open_finance.external_accounts/
 * .external_products) — "NeoFinance" is referenced in the UI's own demo
 * talk-track but has no real data, so it's skipped here.
 */

import { coreApi } from "./client";

const KNOWN_INSTITUTIONS = ["Green Bank", "MongoDB Bank"];
// Shown to the customer in the consent card before anything is connected.
export const CONNECT_INSTITUTIONS = KNOWN_INSTITUTIONS;
export const CONSENT_DURATION_DAYS = 6;
const REQUESTED_PERMISSIONS = ["ACCOUNTS_READ", "ACCOUNTS_BALANCES_READ", "TRANSACTIONS_READ", "LOANS_READ"];

async function getAuthorization(userIdentifier) {
  const { data, error } = await coreApi("openfinance/public/get-authorization", {
    params: { user_identifier: userIdentifier },
  });
  if (error || !data?.BearerToken) {
    return { token: null, error: error || "No BearerToken in response" };
  }
  return { token: data.BearerToken, error: null };
}

async function createAndApproveConsent(bearerToken, userIdentifier, institutionName) {
  const { data: createData, error: createError } = await coreApi("openfinance/secure/consents/", {
    method: "POST",
    bearerToken,
    body: {
      consumer_id: userIdentifier,
      source_institution_name: institutionName,
      expiration_days: CONSENT_DURATION_DAYS,
      permissions: REQUESTED_PERMISSIONS,
    },
  });
  if (createError || !createData?.consent?.ConsentId) {
    return { consentId: null, error: createError || "No ConsentId in response" };
  }
  const consentId = createData.consent.ConsentId;

  const { data: approveData, error: approveError } = await coreApi(
    `openfinance/secure/consents/${encodeURIComponent(consentId)}/approve`,
    { method: "POST", bearerToken },
  );
  if (approveError || approveData?.consent?.Status !== "AUTHORISED") {
    return { consentId: null, error: approveError || "Consent was not authorised" };
  }
  return { consentId, error: null };
}

/**
 * The real fetch-and-cache step — retrieves consent-permitted accounts/
 * products/transactions AND persists them into cachedExternalData, tagged
 * by ConsentId (so revoke/expiry can purge them). Its own response already
 * has everything needed to summarize back to the customer, so no separate
 * cached-data read-back call is needed right after.
 */
async function fetchAndCache(bearerToken, userIdentifier, consentId) {
  const { data, error } = await coreApi(`openfinance/secure/customers/${encodeURIComponent(userIdentifier)}/fetch-and-cache`, {
    method: "POST",
    bearerToken,
    params: { consent_id: consentId },
  });
  if (error) return { accounts: [], products: [], transactions: [], error };
  return {
    accounts: data?.accounts || [],
    products: data?.products || [],
    transactions: data?.transactions || [],
    error: null,
  };
}

/**
 * Connects every known institution with real data for this user, one
 * consent per institution (consents are scoped to a single source
 * institution each — see open-finance-next-gen's consent_service.py), and
 * caches the result server-side via fetch-and-cache.
 *
 * @param {string} userIdentifier - the bank login username (bankUsername)
 * @returns {Promise<{institutions: Array<{institution: string, accounts: any[], products: any[], transactions: any[]}>, errors: string[]}>}
 */
export async function connectAllAccounts(userIdentifier) {
  const { token, error: tokenError } = await getAuthorization(userIdentifier);
  if (tokenError) {
    return { institutions: [], errors: [`Could not authenticate: ${tokenError}`] };
  }

  const institutions = [];
  const errors = [];

  for (const institutionName of KNOWN_INSTITUTIONS) {
    const { consentId, error: consentError } = await createAndApproveConsent(token, userIdentifier, institutionName);
    if (consentError) {
      errors.push(`${institutionName}: ${consentError}`);
      continue;
    }
    const { accounts, products, transactions, error: cacheError } = await fetchAndCache(token, userIdentifier, consentId);
    if (cacheError) {
      errors.push(`${institutionName}: ${cacheError}`);
      continue;
    }
    institutions.push({ institution: institutionName, accounts, products, transactions });
  }

  return { institutions, errors };
}
