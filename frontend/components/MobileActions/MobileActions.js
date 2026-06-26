"use client";

import React, { useState } from "react";
import Icon from "@leafygreen-ui/icon";
import BottomNav from "@/components/BottomNav/BottomNav";
import SendMoneyModal from "@/components/SendMoneyModal/SendMoneyModal";
import LeafyBankAssistant from "@/components/LeafyBankAssistant/LeafyBankAssistant";
import { useRouter } from "next/navigation";

/**
 * Mobile-only quick-actions bar: the bottom navigation plus the modals it
 * launches (Payments, Transactions, Assistant). Self-contained — drop
 * <MobileActions /> onto any page. The bar itself is hidden on desktop via
 * BottomNav's CSS, so the modals are only reachable on mobile.
 */
export default function MobileActions() {
  const [sendMoneyOpen, setSendMoneyOpen] = useState(false);
  const [sendMoneyInitialView, setSendMoneyInitialView] = useState(null);
  const [assistantOpen, setAssistantOpen] = useState(false);

  const openSendMoney = (view) => {
    setSendMoneyInitialView(view);
    setSendMoneyOpen(true);
  };

  const router = useRouter();

  return (
    <>
      {/* Mobile-only bottom navigation. Icons are placeholders — swap the
          `icon` values for the final icons when provided. */}
      <BottomNav
        items={[
          {
            key: "payments",
            label: "Payments",
            icon: <Icon glyph="CreditCard" size="large" />,
            onClick: () => openSendMoney("digital-payment"),
          },
          {
            key: "transactions",
            label: "Transactions",
            icon: <Icon glyph="Coin" size="large" />,
            onClick: () => openSendMoney("transfer"),
          },
          {
            key: "assistant",
            label: "Assistant",
            icon: <Icon glyph="Sparkle" size="large" />,
            onClick: () => setAssistantOpen(true),
          },
          {
            key: "portfolio",
            label: "Portfolio",
            icon: <Icon glyph="Charts" size="large" />,
            onClick: () => router.push("/portfolio"),
          },
        ]}
      />

      <SendMoneyModal
        isOpen={sendMoneyOpen}
        onClose={() => setSendMoneyOpen(false)}
        initialView={sendMoneyInitialView}
      />

      <LeafyBankAssistant
        isOpen={assistantOpen}
        onClose={() => setAssistantOpen(false)}
      />
    </>
  );
}
