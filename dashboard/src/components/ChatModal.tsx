import { useState } from "react";
import type { Conversation, TranscriptTurn } from "../types";
import { Modal } from "./Modal";
import { Badge } from "./Badge";
import {
  ArrowUpRightIcon,
  ChatIcon,
  CheckCircleIcon,
  CopyIcon,
  PhoneIcon,
} from "./Icons";
import { fmtDateTime, fmtDuration, fmtPhone, fmtToken } from "../lib/format";

// WhatsApp-style chat viewer. Mimics the familiar chat layout (customer on
// the left, AI/business on the right, paper-tone backdrop) so transcripts
// read instantly. CTA buttons appear once the conversation has ended.

function initialOf(c: Conversation): string {
  const source = c.customerName || c.customerPhone || "?";
  return source.trim().charAt(0).toUpperCase() || "?";
}

function Bubble({ turn }: { turn: TranscriptTurn }) {
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

export function ChatModal({
  conversation,
  transcript,
  onClose,
}: {
  conversation: Conversation;
  transcript: TranscriptTurn[];
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const c = conversation;
  const ended = !c.live && c.status !== "active";
  const phone = c.customerPhone?.replace(/\D/g, "") ?? null;

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
        {c.channel === "voice" ? <PhoneIcon /> : <ChatIcon />}
        {fmtToken(c.channel)}
      </span>
      <span>· {fmtDateTime(c.startedAt)}</span>
      {c.durationSeconds ? <span>· {fmtDuration(c.durationSeconds)}</span> : null}
      {c.live ? (
        <Badge tone={c.aiPaused ? "warning" : "good"}>
          {c.aiPaused ? "AI paused" : "live"}
        </Badge>
      ) : c.outcome ? (
        <Badge tone={c.outcome === "booked" ? "good" : "neutral"}>{fmtToken(c.outcome)}</Badge>
      ) : null}
    </span>
  );

  return (
    <Modal
      title={c.customerName ?? (c.customerPhone ? fmtPhone(c.customerPhone) : "Unknown customer")}
      subtitle={subtitle}
      onClose={onClose}
      width="max-w-xl"
      footer={
        ended ? (
          <>
            {phone ? (
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
              <span className="text-xs text-muted">No phone number on record</span>
            )}
          </>
        ) : (
          <span className="mr-auto inline-flex items-center gap-2 text-xs text-muted">
            <Badge tone={c.aiPaused ? "warning" : "good"}>
              {c.aiPaused ? "AI paused — owner has taken over" : "Live — AI is handling this chat"}
            </Badge>
          </span>
        )
      }
    >
      <div
        className="min-h-[240px] space-y-2 px-4 py-4"
        style={{ backgroundColor: "var(--chat-bg)" }}
        role="log"
        aria-label={`Chat transcript, ${transcript.length} messages`}
      >
        {/* Legend chip so left/right is never the only signal */}
        <div className="mb-3 flex justify-center">
          <span
            className="rounded-full px-2.5 py-1 text-[10px] font-medium shadow-sm"
            style={{ backgroundColor: "var(--chat-bubble-in)", color: "var(--chat-meta)" }}
          >
            Customer on the left · AI receptionist on the right
          </span>
        </div>
        {transcript.length === 0 ? (
          <div className="flex justify-center py-8">
            <span
              className="rounded-full px-3 py-1.5 text-xs shadow-sm"
              style={{ backgroundColor: "var(--chat-bubble-in)", color: "var(--chat-meta)" }}
            >
              No transcript stored for this conversation
            </span>
          </div>
        ) : (
          transcript.map((t, i) => <Bubble key={i} turn={t} />)
        )}
        {c.summary ? (
          <div className="flex justify-center pt-2">
            <span
              className="max-w-[85%] rounded-lg px-3 py-1.5 text-center text-[11px] shadow-sm"
              style={{ backgroundColor: "var(--chat-bubble-in)", color: "var(--chat-meta)" }}
            >
              Summary: {c.summary}
            </span>
          </div>
        ) : null}
      </div>
      {/* Avatar kept for a11y label symmetry — decorative initial dot */}
      <span className="sr-only">{initialOf(c)}</span>
    </Modal>
  );
}
