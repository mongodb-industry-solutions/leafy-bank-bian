import { Suspense } from "react";
import ShowcaseView from "@/components/Showcase/ShowcaseView";

export const metadata = {
  title: "Leafy Bank — Reconciliation Walkthrough",
};

export default function ShowcasePage() {
  return (
    <Suspense fallback={null}>
      <ShowcaseView />
    </Suspense>
  );
}
