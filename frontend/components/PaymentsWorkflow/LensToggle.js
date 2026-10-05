"use client";

// Business / Technical / Both. Changes layout and copy only; it never refetches. The choice
// persists for the browser session so it survives moving between payments.

import { useEffect, useState } from "react";
import { SegmentedControl, SegmentedControlOption } from "@leafygreen-ui/segmented-control";

const STORAGE_KEY = "payments.lens";
const LENSES = ["business", "technical", "both"];
const DEFAULT_LENS = "both";

export function useLens() {
  const [lens, setLens] = useState(DEFAULT_LENS);

  // Read after mount: sessionStorage does not exist during server render.
  useEffect(() => {
    const saved = window.sessionStorage.getItem(STORAGE_KEY);
    if (LENSES.includes(saved)) setLens(saved);
  }, []);

  function choose(next) {
    setLens(next);
    window.sessionStorage.setItem(STORAGE_KEY, next);
  }

  return [lens, choose];
}

export default function LensToggle({ lens, onChange }) {
  return (
    <SegmentedControl
      aria-label="Audience lens"
      size="xsmall"
      value={lens}
      onChange={onChange}
    >
      <SegmentedControlOption value="business">Business</SegmentedControlOption>
      <SegmentedControlOption value="technical">Technical</SegmentedControlOption>
      <SegmentedControlOption value="both">Both</SegmentedControlOption>
    </SegmentedControl>
  );
}
