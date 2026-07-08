"use client";

// Client shell for the GL Pipeline Monitor page. Owns the single batch-completion
// poll (useBatchTick) and a manual-refresh key, then feeds both the top dashboard
// and the monitor below. Keeping this in one place means one /health poll for the
// whole page, and lets the monitor's "Refresh All" also refresh the dashboard —
// which lives as a sibling and can't be reached from inside the monitor otherwise.
import { useCallback, useState } from "react";
import { useBatchTick } from "@/lib/api/hooks";
import GlDashboardSection from "./GlDashboardSection";
import GlMonitor from "./GlMonitor";

export default function GlPipelineView() {
  const lastBatchAt = useBatchTick();
  const [manualKey, setManualKey] = useState(0);
  const bumpDashboard = useCallback(() => setManualKey((k) => k + 1), []);

  // Dashboard refetches when the batch actually posts (lastBatchAt advances) or
  // when the user hits Refresh All (manualKey bumps).
  const dashboardKey = `${lastBatchAt ?? ""}:${manualKey}`;

  return (
    <>
      <GlDashboardSection refreshKey={dashboardKey} />
      <GlMonitor lastBatchAt={lastBatchAt} onManualRefresh={bumpDashboard} />
    </>
  );
}
