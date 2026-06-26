"use client";

import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import { UserProvider } from "@/lib/context/UserContext";

export function Providers({ children }) {
  return (
    <LeafyGreenProvider>
      <UserProvider>{children}</UserProvider>
    </LeafyGreenProvider>
  );
}
