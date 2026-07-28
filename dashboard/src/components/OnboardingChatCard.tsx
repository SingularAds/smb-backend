import { useState } from "react";
import type { OnboardingChat } from "../types";
import { EmptyState } from "./EmptyState";
import { OnboardingChatModal } from "./OnboardingChatModal";
import { ChatIcon } from "./Icons";
import { fmtNumber, fmtPhone, fmtRelative, fmtToken } from "../lib/format";

// The owner's own onboarding chat with Sofia on the global Recepte number.
// This conversation lives on the onboarding session (not in the business's
// conversations subcollection), so it used to be invisible here — yet for a
// business that never finished pairing it is the ONLY record of what happened.
// Shown for every account that has messages, paired or not.
//
// The viewer modal itself lives in OnboardingChatModal (shared with the
// onboarding-funnel drill-down).

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
          title={`${businessName} — chat with Sofia`}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </section>
  );
}
