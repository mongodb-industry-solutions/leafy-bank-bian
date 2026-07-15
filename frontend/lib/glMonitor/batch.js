// GL monitor batch-window tagging — ported from .docs/gl-monitor-static-ref/js/batch.js.
import { parseTs } from "./format";

// Tags each item CURRENT/LAST relative to the batch windows, keeping the current
// (post-last-batch) window plus the last FIVE batch windows. The windows are
// anchored on lastBatchAt (not wall-clock now), so items persist until new
// batches actually run — they don't vanish just because minutes elapsed. When no
// batch info is known, returns every item untagged (caller shows the full total).
export function filterByBatch(items, tsField, batchInfo) {
  if (!batchInfo || !batchInfo.lastBatchAt) return items.map((x) => ({ ...x, _batchTag: null }));
  const lastEnd = parseTs(batchInfo.lastBatchAt);
  const lastStart = new Date(lastEnd.getTime() - 5 * batchInfo.intervalMs);
  return items
    .map((x) => {
      const t = parseTs(x[tsField]);
      let tag = null;
      if (!isNaN(t)) {
        if (t > lastEnd) tag = "CURRENT";
        else if (t >= lastStart) tag = "LAST";
      }
      return { ...x, _batchTag: tag };
    })
    .filter((x) => x._batchTag !== null);
}
