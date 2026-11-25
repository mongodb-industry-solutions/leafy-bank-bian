import { Suspense } from "react";
import ShowcaseView from "@/components/Showcase/ShowcaseView";

export const metadata = {
  title: "Leafy Bank — Scenarios",
};

export default function ShowcasePage() {
  return (
    <Suspense fallback={null}>
      <ShowcaseView />
    </Suspense>
  );
}
