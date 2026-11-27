"use client";

// A scenario's run survives leaving the page: each stepper keeps its progress (step index and the
// ids it needs to re-poll) in sessionStorage under one key per scenario. Everything else (payments,
// agent records) is re-read from the backend, so only small, JSON-safe state is stored.
// Session-scoped on purpose: a closed tab starts clean, and nothing outlives the browser session.

import { useEffect, useState } from "react";

const PREFIX = "showcase.run.";

function read(scenarioKey) {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(window.sessionStorage.getItem(PREFIX + scenarioKey) || "{}");
  } catch {
    return {};
  }
}

function write(scenarioKey, field, value) {
  const run = { ...read(scenarioKey), [field]: value };
  window.sessionStorage.setItem(PREFIX + scenarioKey, JSON.stringify(run));
}

/** True when the scenario has a run past its first step to return to. */
export const hasSavedRun = (scenarioKey) => (read(scenarioKey).index ?? 0) > 0;

export function clearSavedRun(scenarioKey) {
  window.sessionStorage.removeItem(PREFIX + scenarioKey);
}

/** useState that restores from, and writes back to, the scenario's saved run. */
export function useSavedState(scenarioKey, field, initial) {
  const [value, setValue] = useState(() => {
    const saved = read(scenarioKey);
    return field in saved ? saved[field] : initial;
  });
  useEffect(() => {
    write(scenarioKey, field, value);
  }, [scenarioKey, field, value]);
  return [value, setValue];
}
