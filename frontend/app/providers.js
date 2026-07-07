"use client";

import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import { UserProvider } from "@/lib/context/UserContext";
import { OpenFinanceChatProvider } from "@/lib/context/OpenFinanceChatContext";

export function Providers({ children }) {
  return (
    <LeafyGreenProvider>
      <UserProvider>
        <OpenFinanceChatProvider>{children}</OpenFinanceChatProvider>
      </UserProvider>
    </LeafyGreenProvider>
  );
}
