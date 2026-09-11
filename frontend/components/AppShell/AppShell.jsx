"use client";

import { usePathname } from "next/navigation";
import NavBar from "@/components/NavBar/NavBar";
import FloatingAssistant from "@/components/FloatingAssistant/FloatingAssistant";
import { PaymentsWorkflowProvider } from "@/lib/context/PaymentsWorkflowContext";

const HIDE_ASSISTANT_ROUTES = ["/gl-pipeline-monitor", "/payments-workflow"];

export default function AppShell({ children, bianModelUrl }) {
  const pathname = usePathname();
  const hideAssistant = HIDE_ASSISTANT_ROUTES.some((r) => pathname?.startsWith(r));

  return (
    <PaymentsWorkflowProvider>
      <NavBar bianModelUrl={bianModelUrl} />
      <div className="appContent">{children}</div>
      {!hideAssistant && <FloatingAssistant />}
    </PaymentsWorkflowProvider>
  );
}
