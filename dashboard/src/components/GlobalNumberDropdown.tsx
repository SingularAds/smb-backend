import { useEffect, useRef, useState } from "react";
import type { GlobalNumberOption } from "../types";
import { CheckBoldIcon, ChevronDownIcon, PhoneIcon } from "./Icons";
import { fmtNumber, fmtPhone } from "../lib/format";

// Global-number picker. Onboarding can run on several global WhatsApp numbers;
// this scopes the WHOLE overview (funnel, KPIs, accounts) to one of them.
// Mirrors DateRangeDropdown's composition: rows with a bold check on the
// selection, ghost hover wash, counts as secondary text.

export const ALL_NUMBERS = "__all__";

function optionLabel(o: GlobalNumberOption): string {
  return o.number ? fmtPhone(o.number) : o.label;
}

export function GlobalNumberDropdown({
  options,
  value,
  onChange,
}: {
  options: GlobalNumberOption[];
  /** null = all numbers combined */
  value: string | null;
  onChange: (deviceId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // With a single configured number and no legacy bucket there is nothing to
  // switch between — don't add a control that can only have one value.
  if (options.length <= 1) return null;

  const selected = options.find((o) => o.deviceId === value) ?? null;
  const buttonLabel = selected ? optionLabel(selected) : "All numbers";
  const totalAccounts = options.reduce((sum, o) => sum + o.accounts, 0);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-lg border border-hairline bg-surface px-3 py-1.5 text-xs font-medium text-ink-2 hover:text-ink"
        style={{ boxShadow: "var(--shadow-card)" }}
        title="Scope the whole screen to one global onboarding number"
      >
        <PhoneIcon className="text-muted" />
        {buttonLabel}
        <span className={open ? "rotate-180" : ""}>
          <ChevronDownIcon className="text-muted" />
        </span>
      </button>

      {open ? (
        <div
          role="dialog"
          aria-label="Choose global onboarding number"
          className="absolute left-0 z-40 mt-1.5 w-72 overflow-hidden rounded-lg border border-hairline bg-surface"
          style={{ boxShadow: "var(--shadow-modal)" }}
        >
          <p className="border-b border-hairline px-3 py-2 text-[11px] text-muted">
            Onboarding number — scopes the funnel, KPIs and accounts below.
          </p>
          <ul className="max-h-72 overflow-y-auto py-1">
            <li>
              <button
                type="button"
                onClick={() => {
                  onChange(null);
                  setOpen(false);
                }}
                aria-pressed={value === null}
                className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-ink-2 hover:bg-page"
              >
                <span className={value === null ? "font-semibold text-ink" : ""}>
                  All numbers
                </span>
                <span className="flex items-center gap-2">
                  <span className="text-[11px] text-muted">
                    {fmtNumber(totalAccounts)} accounts
                  </span>
                  {value === null ? <CheckBoldIcon className="text-accent" /> : null}
                </span>
              </button>
            </li>
            {options.map((o) => {
              const active = value === o.deviceId;
              return (
                <li key={o.deviceId}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange(o.deviceId);
                      setOpen(false);
                    }}
                    aria-pressed={active}
                    className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-ink-2 hover:bg-page"
                  >
                    <span className="min-w-0">
                      <span className={`block truncate ${active ? "font-semibold text-ink" : ""}`}>
                        {optionLabel(o)}
                      </span>
                      <span className="block text-[11px] text-muted">
                        {fmtNumber(o.accounts)} accounts · {fmtNumber(o.sessions)} chats
                      </span>
                    </span>
                    {active ? <CheckBoldIcon className="shrink-0 text-accent" /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
