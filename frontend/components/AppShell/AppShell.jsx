"use client";

import { usePathname } from "next/navigation";
import NavBar from "@/components/NavBar/NavBar";
import FloatingAssistant from "@/components/FloatingAssistant/FloatingAssistant";

const HIDE_ASSISTANT_ROUTES = ["/gl-pipeline-monitor"];

export default function AppShell({ children, bianModelUrl }) {
  const pathname = usePathname();
  const hideAssistant = HIDE_ASSISTANT_ROUTES.some((r) => pathname?.startsWith(r));

  return (
    <>
      <NavBar bianModelUrl={bianModelUrl} />
      <div className="appContent">{children}</div>
      {!hideAssistant && <FloatingAssistant />}
    </>
  );
}
