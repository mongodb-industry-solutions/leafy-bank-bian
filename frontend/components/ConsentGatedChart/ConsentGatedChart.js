"use client";

import { useEffect, useRef } from "react";
import ChartsEmbedSDK from "@mongodb-js/charts-embed-dom";
import { useUser } from "@/lib/context/UserContext";

const sdk = new ChartsEmbedSDK({
  baseUrl: "https://charts.mongodb.com/charts-jeffn-zsdtj",
});

/**
 * Atlas Charts embed for consent-gated data (cachedExternalData).
 *
 * Renders nothing until the current browser session holds an active consent,
 * so the chart never shows before consent is granted. Once consent exists, the
 * chart is scoped to THIS session's consent IDs via an injected filter, so data
 * cached by other sessions for the same user never leaks in.
 *
 * Requires the chart to have unauthenticated embedding enabled and `ConsentId`
 * whitelisted as a filterable field in Atlas Charts.
 */
export default function ConsentGatedChart({ chartId, width = "100%", height = "100%" }) {
  const { hasActiveConsent, authorizedConsentIds, consentRefreshKey } = useUser();
  const containerRef = useRef(null);

  useEffect(() => {
    if (!hasActiveConsent || !containerRef.current) return;

    const chart = sdk.createChart({
      chartId,
      maxDataAge: 60,
      theme: "light",
      autoRefresh: true,
      filter: { ConsentId: { $in: authorizedConsentIds } },
    });

    chart.render(containerRef.current);

    return () => {
      if (containerRef.current) containerRef.current.innerHTML = "";
    };
    // Re-render when the session's consents change.
  }, [chartId, hasActiveConsent, authorizedConsentIds, consentRefreshKey]);

  if (!hasActiveConsent) return null;

  return <div ref={containerRef} style={{ width, height }} />;
}
