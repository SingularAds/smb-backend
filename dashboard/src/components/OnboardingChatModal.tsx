import { useState } from "react";
import type { OnboardingChat, TranscriptTurn } from "../types";
import { Modal } from "./Modal";
import { Badge } from "./Badge";
import {
  ArrowUpRightIcon,
  ChatIcon,
  CheckCircleIcon,
  CopyIcon,
} from "./Icons";
import { fmtDateTime, fmtNumber, fmtPhone, fmtToken } from "../lib/format";

// The owner's own onboarding chat with Sofia on the global Recepte number,
// read from the append-only onboarding_transcripts archive (every message,
// templates included). Extracted from OnboardingChatCard so both the
// business-detail card and the funnel drill-down can open the same viewer.

function Bubble({ turn }: { turn: TranscriptTurn }) {
  // Sofia (the AI) on the right, the business owner on the left — same
  // orientation as the customer-conversation viewer.
  const isAi = turn.role === "assistant";
  return (
    <div className={`flex ${isAi ? "justify-end" : "justify-start"}`}>
      <div
        className="max-w-[78%] rounded-lg px-3 py-2 text-sm shadow-sm"
        style={{
          backgroundColor: isAi ? "var(--chat-bubble-out)" : "var(--chat-bubble-in)",
          color: "var(--chat-ink)",
          borderTopRightRadius: isAi ? 2 : undefined,
          borderTopLeftRadius: isAi ? undefined : 2,
        }}
      >
        <p className="whitespace-pre-wrap break-words">{turn.text}</p>
        {turn.ts ? (
          <p className="mt-0.5 text-right text-[10px]" style={{ color: "var(--chat-meta)" }}>
            {fmtDateTime(turn.ts)}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function OnboardingChatModal({
  chat,
  title,
  onClose,
}: {
  chat: OnboardingChat;
  /** dialog heading — e.g. the business name, or the owner's name/phone */
  title: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const phone = chat.ownerPhone?.replace(/\D/g, "") ?? null;

  async function copyPhone() {
    if (!phone) return;
    try {
      await navigator.clipboard.writeText(phone);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable — button simply doesn't confirm */
    }
  }

  const subtitle = (
    <span className="flex flex-wrap items-center gap-2">
      <span className="inline-flex items-center gap-1">
        <ChatIcon />
        Onboarding chat
      </span>
      {chat.onboardingNumber ? (
        <span>· on {fmtPhone(chat.onboardingNumber)}</span>
      ) : null}
      <span>· {fmtNumber(chat.messageCount)} messages</span>
      {chat.currentStep ? (
        <Badge tone="neutral">{fmtToken(chat.currentStep)}</Badge>
      ) : null}
    </span>
  );

  return (
    <Modal
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      width="max-w-xl"
      footer={
        phone ? (
          <>
            <button
              type="button"
              onClick={copyPhone}
              className="inline-flex items-center gap-1.5 rounded-md border border-hairline px-3 py-1.5 text-xs font-medium text-ink-2 hover:bg-page"
            >
              {copied ? <CheckCircleIcon /> : <CopyIcon />}
              {copied ? "Copied" : "Copy number"}
            </button>
            <a
              href={`https://wa.me/${phone}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              <ArrowUpRightIcon />
              Open in WhatsApp
            </a>
          </>
        ) : (
          <span className="text-xs text-muted">No owner phone on record</span>
        )
      }
    >
      <div
        className="min-h-[240px] space-y-2 px-4 py-4"
        style={{ backgroundColor: "var(--chat-bg)" }}
        role="log"
        aria-label={`Onboarding chat, ${chat.turns.length} messages`}
      >
        <div className="mb-3 flex justify-center">
          <span
            className="rounded-full px-2.5 py-1 text-[10px] font-medium shadow-sm"
            style={{ backgroundColor: "var(--chat-bubble-in)", color: "var(--chat-meta)" }}
          >
            Business owner on the left · Sofia on the right
          </span>
        </div>
        {chat.turns.map((t, i) => (
          <Bubble key={i} turn={t} />
        ))}
      </div>
    </Modal>
  );
}
