import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "./Modal";
import { OnboardingChatViewer } from "./OnboardingChatViewer";
import { ChatIcon } from "./Icons";
import type { FunnelSessionEntry } from "../types";
import { fmtPhone, fmtRelative, fmtToken } from "../lib/format";

// Drill-down modal: shows the individual owners at a specific funnel stage.
// Clicking "View" on a completed owner navigates to the Business Detail page.
// For in-progress owners (no businessId), phone + step are shown for direct
// WhatsApp contact.

function copyToClipboard(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

export function FunnelDrillModal({
  label,
  sessions,
  onClose,
}: {
  label: string;
  sessions: FunnelSessionEntry[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [copied, setCopied] = useState<string | null>(null);
  // The prospect whose onboarding conversation is being viewed (null = none).
  const [chatFor, setChatFor] = useState<FunnelSessionEntry | null>(null);

  function handleCopy(phone: string) {
    copyToClipboard(phone);
    setCopied(phone);
    setTimeout(() => setCopied(null), 1500);
  }

  function handleView(businessId: string) {
    onClose();
    navigate(`/business/${encodeURIComponent(businessId)}`);
  }

  return (
    <Modal
      title={`${label}`}
      subtitle={`${sessions.length} owner${sessions.length !== 1 ? "s" : ""} at this stage — most recent first`}
      onClose={onClose}
      width="max-w-2xl"
    >
      {sessions.length === 0 ? (
        <div className="px-5 py-10 text-center text-sm text-muted">
          No sessions at this stage in the selected range.
        </div>
      ) : (
        <ul className="divide-y divide-[var(--hairline)] px-0">
          {sessions.map((s, i) => (
            <li
              key={s.phone ?? i}
              className="flex items-start gap-3 px-5 py-3.5"
            >
              {/* Avatar initial */}
              <div
                className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold"
                style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
                aria-hidden="true"
              >
                {(s.name ?? s.phone ?? "?")[0].toUpperCase()}
              </div>

              {/* Main info */}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-sm font-medium text-ink">
                    {s.name ?? "Unknown owner"}
                  </span>
                  {s.businessName ? (
                    <span className="text-xs text-muted">{s.businessName}</span>
                  ) : null}
                </div>

                <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
                  {/* Phone — click to copy */}
                  {s.phone ? (
                    <button
                      type="button"
                      onClick={() => handleCopy(fmtPhone(s.phone))}
                      title="Click to copy"
                      className="flex items-center gap-1 rounded px-1 hover:bg-[var(--page)] hover:text-ink transition-colors"
                    >
                      <span>{fmtPhone(s.phone)}</span>
                      <span className="text-[10px]">
                        {copied === fmtPhone(s.phone) ? "✓" : "⎘"}
                      </span>
                    </button>
                  ) : null}

                  {/* Current step */}
                  {s.currentStep ? (
                    <span
                      className="rounded-full border border-[var(--hairline)] px-1.5 py-px font-mono text-[10px]"
                    >
                      {fmtToken(s.currentStep)}
                    </span>
                  ) : null}

                  {/* Reconstructed: no live onboarding session survives for this
                      owner — this row was rebuilt from the business record. */}
                  {s.reconstructed ? (
                    <span
                      className="rounded-full border border-[var(--hairline)] px-1.5 py-px text-[10px] text-muted"
                      title="The original onboarding chat session no longer exists (deleted after completion). This row was reconstructed from the business record."
                    >
                      reconstructed
                    </span>
                  ) : null}

                  {/* Acquisition context — campaign / ad id when attributed */}
                  {s.campaign ? (
                    <span
                      className="rounded-full px-1.5 py-px text-[10px]"
                      style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
                      title="Campaign"
                    >
                      {s.campaign}
                    </span>
                  ) : null}
                  {s.adId ? (
                    <span className="font-mono text-[10px] text-muted" title="Ad ID">
                      ad&nbsp;{s.adId}
                    </span>
                  ) : null}

                  {/* Started relative time */}
                  {s.startedAt ? (
                    <span title={s.startedAt}>{fmtRelative(s.startedAt)}</span>
                  ) : null}
                </div>
              </div>

              {/* Action buttons */}
              <div className="flex shrink-0 items-center gap-1.5">
                {/* View the onboarding conversation (from the transcript
                    archive) — works even for prospects with no business page. */}
                {s.phone ? (
                  <button
                    type="button"
                    onClick={() => setChatFor(s)}
                    title="View onboarding conversation"
                    className="inline-flex items-center gap-1 rounded-md border border-[var(--hairline)] px-2 py-1 text-xs font-medium text-ink-2 hover:bg-[var(--page)] hover:text-ink"
                  >
                    <ChatIcon />
                    Chat
                  </button>
                ) : null}

                {s.businessId ? (
                  <button
                    type="button"
                    onClick={() => handleView(s.businessId!)}
                    className="rounded-md px-2.5 py-1 text-xs font-medium"
                    style={{
                      background: "var(--accent)",
                      color: "#fff",
                    }}
                  >
                    View →
                  </button>
                ) : (
                  <a
                    href={`https://wa.me/${(s.phone ?? "").replace(/\D/g, "")}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded-md border border-[var(--hairline)] px-2.5 py-1 text-xs font-medium text-ink-2 hover:bg-[var(--page)]"
                  >
                    WhatsApp ↗
                  </a>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {chatFor?.phone ? (
        <OnboardingChatViewer
          phone={chatFor.phone}
          title={`${chatFor.name ?? chatFor.businessName ?? fmtPhone(chatFor.phone)} — onboarding chat`}
          onClose={() => setChatFor(null)}
        />
      ) : null}
    </Modal>
  );
}
