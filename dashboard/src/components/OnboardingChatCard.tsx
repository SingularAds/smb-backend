import { useState } from "react";
import type { OnboardingChat, TranscriptTurn } from "../types";
import { Modal } from "./Modal";
import { Badge } from "./Badge";
import { EmptyState } from "./EmptyState";
import {
  ArrowUpRightIcon,
  ChatIcon,
  CheckCircleIcon,
  CopyIcon,
} from "./Icons";
import { fmtDateTime, fmtNumber, fmtPhone, fmtRelative, fmtToken } from "../lib/format";

// The owner's own onboarding chat with Sofia on the global Recepte number.
// This conversation lives on the onboarding session (not in the business's
// conversations subcollection), so it used to be invisible here — yet for a
// business that never finished pairing it is the ONLY record of what happened.
// Shown for every account that has messages, paired or not.

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

function OnboardingChatModal({
  chat,
  businessName,
  onClose,
}: {
  chat: OnboardingChat;
  businessName: string;
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
      title={`${businessName} — chat with Sofia`}
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

export function OnboardingChatCard({
  chat,
  businessName,
  whatsappPaired,
  style,
}: {
  chat: OnboardingChat | null;
  businessName: string;
  whatsappPaired: boolean;
  style?: React.CSSProperties;
}) {
  const [open, setOpen] = useState(false);

  return (
    <section className="card rise p-4" style={style}>
      <h2 className="mb-1 text-sm font-semibold text-ink">
        Onboarding chat
        {chat ? (
          <span className="ml-2 font-normal text-muted">
            {fmtNumber(chat.messageCount)} messages
          </span>
        ) : null}
      </h2>
      {!chat ? (
        <EmptyState
          title="No onboarding chat found"
          explanation="This is the owner's own conversation with Sofia on the Recepte number. Nothing is stored for this account — it was likely created directly (import or API) rather than through a WhatsApp onboarding chat."
        />
      ) : (
        <>
          <p className="mb-3 text-xs text-muted">
            The owner's own conversation with Sofia on the Recepte number
            {chat.onboardingNumber ? ` (${fmtPhone(chat.onboardingNumber)})` : ""} — the
            chat that onboarded them
            {whatsappPaired ? "" : ", still unfinished (never paired)"}.
          </p>
          <dl className="mb-3 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted">Messages</dt>
              <dd className="mt-0.5 text-sm text-ink-2">
                {fmtNumber(chat.messageCount)}
                <span className="text-muted">
                  {" "}
                  ({fmtNumber(chat.ownerMessageCount)} from owner)
                </span>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Stopped at</dt>
              <dd className="mt-0.5 text-sm text-ink-2">
                {chat.currentStep ? fmtToken(chat.currentStep) : "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Language</dt>
              <dd className="mt-0.5 text-sm text-ink-2">{chat.language ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Last message</dt>
              <dd className="mt-0.5 text-sm text-ink-2">
                {fmtRelative(chat.lastActivityAt)}
              </dd>
            </div>
          </dl>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            <ChatIcon />
            Read the full conversation
          </button>
        </>
      )}
      {open && chat ? (
        <OnboardingChatModal
          chat={chat}
          businessName={businessName}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </section>
  );
}
