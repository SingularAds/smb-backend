import { useEffect, useRef, useState } from "react";
import type { RangeFilter, RangePreset } from "../types";
import { CalendarIcon, CheckBoldIcon, ChevronDownIcon } from "./Icons";
import { fmtDate } from "../lib/format";

// Date-range dropdown: preset rows first (selection marked with a bold check,
// hover is a ghost wash), custom range behind a hairline in the footer —
// the composition the dataviz interaction spec describes.

const PRESETS: { value: RangePreset; label: string }[] = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
  { value: 365, label: "Last 12 months" },
];

function filterLabel(filter: RangeFilter): string {
  if (filter.from) {
    const from = fmtDate(filter.from);
    const to = filter.to ? fmtDate(filter.to) : "today";
    return `${from} – ${to}`;
  }
  return PRESETS.find((p) => p.value === filter.preset)?.label ?? "Last 30 days";
}

export function DateRangeDropdown({
  filter,
  onChange,
}: {
  filter: RangeFilter;
  onChange: (f: RangeFilter) => void;
}) {
  const [open, setOpen] = useState(false);
  const [from, setFrom] = useState(filter.from ?? "");
  const [to, setTo] = useState(filter.to ?? "");
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

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-lg border border-hairline bg-surface px-3 py-1.5 text-xs font-medium text-ink-2 hover:text-ink"
        style={{ boxShadow: "var(--shadow-card)" }}
      >
        <CalendarIcon className="text-muted" />
        {filterLabel(filter)}
        <span className={open ? "rotate-180" : ""}>
          <ChevronDownIcon className="text-muted" />
        </span>
      </button>

      {open ? (
        <div
          role="dialog"
          aria-label="Choose date range"
          className="absolute right-0 z-40 mt-1.5 w-64 overflow-hidden rounded-lg border border-hairline bg-surface"
          style={{ boxShadow: "var(--shadow-modal)" }}
        >
          <ul className="py-1">
            {PRESETS.map((p) => {
              const active = !filter.from && filter.preset === p.value;
              return (
                <li key={p.value}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange({ preset: p.value, from: null, to: null });
                      setOpen(false);
                    }}
                    aria-pressed={active}
                    className="flex w-full items-center justify-between px-3 py-2 text-left text-xs text-ink-2 hover:bg-page"
                  >
                    <span className={active ? "font-semibold text-ink" : ""}>{p.label}</span>
                    {active ? <CheckBoldIcon className="text-accent" /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="border-t border-hairline px-3 py-3">
            <p className="mb-2 text-[11px] font-medium text-muted">
              Custom range {filter.from ? <CheckBoldIcon className="ml-1 text-accent" /> : null}
            </p>
            <div className="space-y-2">
              <label className="block text-[11px] text-muted">
                From
                <input
                  type="date"
                  value={from}
                  onChange={(e) => setFrom(e.target.value)}
                  className="mt-0.5 w-full rounded border border-hairline bg-page px-2 py-1 text-xs text-ink"
                />
              </label>
              <label className="block text-[11px] text-muted">
                To <span className="opacity-70">(optional — defaults to today)</span>
                <input
                  type="date"
                  value={to}
                  onChange={(e) => setTo(e.target.value)}
                  className="mt-0.5 w-full rounded border border-hairline bg-page px-2 py-1 text-xs text-ink"
                />
              </label>
              <button
                type="button"
                disabled={!from}
                onClick={() => {
                  onChange({ preset: null, from, to: to || null });
                  setOpen(false);
                }}
                className="w-full rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
              >
                Apply custom range
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
