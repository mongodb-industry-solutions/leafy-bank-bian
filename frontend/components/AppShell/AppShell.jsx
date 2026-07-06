"use client";

import { usePathname } from "next/navigation";
import NavBar from "@/components/NavBar/NavBar";
import FloatingAssistant from "@/components/FloatingAssistant/FloatingAssistant";

export default function AppShell({ children, bianModelUrl }) {
  const pathname = usePathname();

  return (
    <>
      <NavBar bianModelUrl={bianModelUrl} />
      <div className="appContent">{children}</div>
      <FloatingAssistant />
    </>
  );
}
