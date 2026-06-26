"use client";

// Alias route so cross-service links that point at /bian-data-model still
// resolve. Root "/" renders the same explorer.
import BianDataModelPage from "@/components/BianDataModel/BianDataModelPage";

export default function Page() {
  return <BianDataModelPage />;
}