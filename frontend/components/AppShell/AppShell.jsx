"use client";

// Wraps the app chrome (global NavBar + FloatingAssistant) so it can be hidden
// on full-screen back-office routes that bring their own header, e.g. the GL
// Pipeline Monitor. Otherwise the global NavBar overlaps the page's own TopBar.
import { usePathname } from "next/navigation";
import NavBar from "@/components/NavBar/NavBar";
import FloatingAssistant from "@/components/FloatingAssistant/FloatingAssistant";

const BARE_ROUTES = ["/gl-pipeline-monitor"];

export default function AppShell({ children, bianModelUrl }) {
  const pathname = usePathname();
  const bare = BARE_ROUTES.some((r) => pathname?.startsWith(r));

  if (bare) {
    // No fixed NavBar here, so skip the .appContent 64px top offset that
    // reserves space for it — otherwise it leaves a white gap.
    return <>{children}</>;
  }

  return (
    <>
      <NavBar bianModelUrl={bianModelUrl} />
      <div className="appContent">{children}</div>
      <FloatingAssistant />
    </>
  );
}
