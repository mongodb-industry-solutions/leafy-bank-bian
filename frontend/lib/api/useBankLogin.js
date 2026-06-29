"use client";

import { useState, useEffect, useCallback } from "react";
import { coreApi, chatStream } from "./client";
import { useUser } from "@/lib/context/UserContext";

export function useBankLogin({ consentId, institutionName, threadId }) {
  const { selectedUser, updateBearerToken } = useUser();

  // State
  const [step, setStep] = useState("login"); // "login" | "fetching" | "consent" | "processing" | "done"
  const [token, setToken] = useState(null);
  const [tokenError, setTokenError] = useState(null);
  const [consentData, setConsentData] = useState(null);
  const [statusText, setStatusText] = useState("");
  const [encryptionDemo, setEncryptionDemo] = useState(null);
  const [encryptionLoading, setEncryptionLoading] = useState(false);

  // Validity check — params may be null from search params
  const isValid = !!consentId && !!institutionName && !!threadId;

  // Fetch encryption demo when consent step is reached
  useEffect(() => {
    if (step !== "consent" || !consentData?.consent_id) return;

    setEncryptionLoading(true);
    const encodedId = encodeURIComponent(consentData.consent_id);
    coreApi(`encryption-demo/compare/${encodedId}`).then(({ data, error }) => {
      if (data && !error) {
        setEncryptionDemo(data);
      }
      setEncryptionLoading(false);
    });
  }, [step, consentData?.consent_id]);

  // Auto-fetch bearer token on mount
  useEffect(() => {
    if (!selectedUser?.name) return;

    setStep("fetching");
    setStatusText("Connecting to bank...");

    coreApi(
      "openfinance/public/get-authorization",
      { params: { user_identifier: selectedUser.name } }
    ).then(({ data, error }) => {
      if (data?.BearerToken) {
        setToken(data.BearerToken);
        updateBearerToken(data.BearerToken);
        setStep("login");
        setStatusText("");
      } else {
        setTokenError(error || "Could not retrieve authorization token");
        setStep("login");
      }
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUser?.name]);

  // Process SSE stream to extract consent interrupt
  const processSSEForConsent = useCallback(async (response) => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          for (const line of part.split("\n")) {
            if (line.startsWith("data: ")) {
              try {
                const event = JSON.parse(line.slice(6));
                if (event.type === "interrupt" && event.payload?.type === "CONSENT_APPROVAL") {
                  setConsentData(event.payload);
                  setStep("consent");
                  setStatusText("");
                } else if (event.type === "response") {
                  // Agent responded without interrupt — done
                  setStep("done");
                  setStatusText("Complete");
                } else if (event.type === "status") {
                  setStatusText(event.payload?.message || "Processing...");
                }
              } catch {
                // skip malformed
              }
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }, []);

  // Step 1: Simulate bank login — resume the BANK_LOGIN interrupt
  async function handleBankLogin() {
    if (!threadId || !selectedUser?.name) return;

    setStep("processing");
    setStatusText("Authenticating with bank...");

    try {
      const res = await chatStream("chat/stream/resume", {
        thread_id: threadId,
        user_id: selectedUser.name,
        resume_data: { status: "success" },
        profile: null,
      });
      await processSSEForConsent(res);
    } catch (e) {
      setTokenError(`Login failed: ${e.message}`);
      setStep("login");
    }
  }

  // Step 2: Handle consent approval/decline
  async function handleConsentDecision(approved) {
    if (!threadId || !selectedUser?.name) return;

    setStep("processing");
    setStatusText(approved ? "Approving consent..." : "Declining consent...");

    try {
      const res = await chatStream("chat/stream/resume", {
        thread_id: threadId,
        user_id: selectedUser.name,
        resume_data: { approved },
        profile: null,
      });

      // Parse SSE stream to extract the final AI response and suggestions
      let finalResponse = null;
      let finalSuggestions = null;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop();

          for (const part of parts) {
            for (const line of part.split("\n")) {
              if (line.startsWith("data: ")) {
                try {
                  const event = JSON.parse(line.slice(6));
                  if (event.type === "response") {
                    finalResponse = event.payload?.text || null;
                  } else if (event.type === "suggestions") {
                    finalSuggestions = event.payload?.items || null;
                  }
                } catch {
                  // skip malformed
                }
              }
            }
          }
        }
      } finally {
        reader.releaseLock();
      }

      // Send completion to the original tab's chatbot via BroadcastChannel
      const channel = new BroadcastChannel("leafy-bank-consent");
      channel.postMessage({
        type: approved ? "consent_complete" : "consent_declined",
        _broadcastId: crypto.randomUUID(),
        response: finalResponse,
        suggestions: finalSuggestions,
        consentId: consentData?.consent_id,
        institution: consentData?.source_institution || institutionName,
        bearerToken: approved ? token : null,
      });
      channel.close();

      setStep("done");
      setStatusText(approved ? "Consent approved!" : "Consent declined.");
    } catch (e) {
      setTokenError(`Error: ${e.message}`);
      setStep("consent");
    }
  }

  // Close this tab (opened from chatbot)
  function handleCloseTab() {
    window.close();
  }

  return {
    // State
    step,
    token,
    tokenError,
    consentData,
    statusText,
    isValid,
    encryptionDemo,
    encryptionLoading,
    // Handlers
    handleBankLogin,
    handleConsentDecision,
    handleCloseTab,
  };
}
