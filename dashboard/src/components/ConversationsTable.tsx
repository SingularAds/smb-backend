import { useState } from "react";
import type { Conversation, TranscriptTurn } from "../types";
import { Badge } from "./Badge";
import { ChatModal } from "./ChatModal";
import { ChatIcon, PhoneIcon } from "./Icons";
import { fmtDateTime, fmtDuration, fmtNumber, fmtPhone, fmtToken } from "../lib/format";

// Clean conversations table: click a row (or its "Open" button, for keyboard
// users) to read the full chat in the WhatsApp-style modal. Sorting is fixed
// newest-first (the backend guarantees it).

function OutcomeBadge({ outcome }: { outcome: string | null }) {
  switch (outcome) {
    case "booked":
      return <Badge tone="good">booked</Badge>;
    case "missed":
      return <Badge tone="serious">missed</Badge>;
    case "transferred":
      return <Badge tone="neutral">transferred</Badge>;
    case "pending":
      return <Badge tone="warning">pending</Badge>;
    case null:
      return <span className="text-muted">—</span>;
    default:
      return <Badge tone="neutral">{fmtToken(outcome)}</Badge>;
  }
}

function Avatar({ c }: { c: Conversation }) {
  const source = c.customerName || c.customerPhone || "?";
  const initial = source.trim().charAt(0).toUpperCase() || "?";
  return (
    <span
      aria-hidden="true"
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold text-accent"
      style={{ backgroundColor: "var(--accent-soft)" }}
    >
      {initial}
    </span>
  );
}

export function ConversationsTable({
  conversations,
  transcripts,
}: {
  conversations: Conversation[];
  transcripts: Record<string, TranscriptTurn[]>;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const open = conversations.find((c) => c.id === openId) ?? null;

  const th = "sticky top-0 z-10 bg-surface py-2 pr-3 font-medium shadow-[inset_0_-1px_0_var(--gridline)]";
  return (
    // ~5 rows visible, then scroll within the card (header stays pinned).
    <div
      className="max-h-[19rem] overflow-auto overscroll-contain"
      tabIndex={0}
      role="group"
      aria-label="Conversations in the selected range, newest first"
    >
      <table className="w-full text-sm">
        <caption className="sr-only">
          Conversations in the selected range, newest first. Open a row to read
          the chat.
        </caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className={th}>Customer</th>
            <th scope="col" className={th}>Channel</th>
            <th scope="col" className={th}>Started</th>
            <th scope="col" className={th}>Outcome</th>
            <th scope="col" className={`${th} text-right`}>Duration</th>
            <th scope="col" className={`${th} text-right`}>Msgs</th>
            <th scope="col" className={`${th} w-16 text-right`}>
              <span className="sr-only">Open chat</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {conversations.length === 0 ? (
            <tr>
              <td colSpan={7} className="py-6 text-center text-muted">
                No conversations in this range
              </td>
            </tr>
          ) : (
            conversations.map((c) => (
              <tr
                key={c.id}
                className="cursor-pointer border-b border-hairline last:border-0 hover:bg-page"
                onClick={() => setOpenId(c.id)}
              >
                <td className="py-2 pr-3">
                  <span className="flex items-center gap-2.5">
                    <Avatar c={c} />
                    <span className="min-w-0">
                      <span className="block truncate font-medium text-ink">
                        {c.customerName ?? (c.customerPhone ? fmtPhone(c.customerPhone) : "Unknown")}
                      </span>
                      {c.customerName && c.customerPhone ? (
                        <span className="block text-xs text-muted">{fmtPhone(c.customerPhone)}</span>
                      ) : null}
                    </span>
                  </span>
                </td>
                <td className="py-2 pr-3">
                  <span className="inline-flex items-center gap-1.5 text-ink-2">
                    {c.channel === "voice" ? <PhoneIcon /> : <ChatIcon />}
                    {fmtToken(c.channel)}
                    {c.live ? (
                      <Badge tone={c.aiPaused ? "warning" : "good"}>
                        {c.aiPaused ? "AI paused" : "live"}
                      </Badge>
                    ) : null}
                  </span>
                </td>
                <td className="whitespace-nowrap py-2 pr-3 text-ink-2">
                  {fmtDateTime(c.startedAt)}
                </td>
                <td className="py-2 pr-3">
                  <OutcomeBadge outcome={c.outcome} />
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-ink-2">
                  {fmtDuration(c.durationSeconds)}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-ink-2">
                  {fmtNumber(c.messageCount)}
                </td>
                <td className="py-2 text-right">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setOpenId(c.id);
                    }}
                    className="rounded-md border border-hairline px-2 py-1 text-xs font-medium text-ink-2 hover:bg-page hover:text-ink"
                    aria-label={`Open chat with ${c.customerName ?? c.customerPhone ?? "unknown customer"} started ${fmtDateTime(c.startedAt)}`}
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
      {open ? (
        <ChatModal
          conversation={open}
          transcript={transcripts[open.id] ?? []}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}
