import { useEffect, useState } from "react";
import type { OnboardingChat } from "../types";
import { ApiError, fetchOnboardingChat } from "../api";
import { Modal } from "./Modal";
import { OnboardingChatModal } from "./OnboardingChatModal";

// Fetches one prospect's onboarding conversation by phone (lazily, on open)
// and shows it in the shared OnboardingChatModal. While loading — or when
// there is no transcript / an error — it renders a small placeholder modal so
// the click always gives immediate feedback. Used by the funnel drill-down,
// where the prospect may have no business page to link to.

export function OnboardingChatViewer({
  phone,
  title,
  onClose,
}: {
  phone: string;
  /** dialog heading — the prospect's name or phone */
  title: string;
  onClose: () => void;
}) {
  const [chat, setChat] = useState<OnboardingChat | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">(
    "loading",
  );
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    fetchOnboardingChat(phone).then(
      (data) => {
        if (cancelled) return;
        setChat(data);
        setState("ready");
      },
      (err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setState("empty");
          return;
        }
        setMessage(
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : "Could not load the conversation.",
        );
        setState("error");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [phone]);

  if (state === "ready" && chat) {
    return <OnboardingChatModal chat={chat} title={title} onClose={onClose} />;
  }

  const body =
    state === "loading" ? (
      <p className="text-sm text-muted">Loading conversation…</p>
    ) : state === "empty" ? (
      <p className="text-sm text-muted">
        No onboarding conversation is stored for this prospect. Their messages
        were either never archived (a very early onboarding) or the chat has
        not started yet.
      </p>
    ) : (
      <p role="alert" className="text-sm text-[var(--negative,#b3261e)]">
        {message}
      </p>
    );

  return (
    <Modal title={title} subtitle="Onboarding chat" onClose={onClose} width="max-w-md">
      <div className="px-5 py-8 text-center">{body}</div>
    </Modal>
  );
}
