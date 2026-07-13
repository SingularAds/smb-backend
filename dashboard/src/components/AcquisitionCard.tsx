import { useState } from "react";
import type { AcquisitionChannel } from "../types";
import { FunnelDrillModal } from "./FunnelDrillModal";
import { fmtNumber, fmtPercent } from "../lib/format";

// Acquisition-by-channel: how many onboarding PROSPECTS (owners who messaged the
// global onboarding number) arrived via each marketing channel — Meta ads first
// (the client's focus), then website/organic. Each channel shows the same
// started → connected → onboarded steps as the funnel; clicking opens a modal
// listing those prospects and their current step.
//
// Only prospects with attribution are counted, so the card stays hidden until
// real ad/website leads exist (Overview guards on byChannel.length).

const RAMP = [
  "var(--ordinal-1)",
  "var(--ordinal-2)",
  "var(--ordinal-3)",
  "var(--ordinal-4)",
];

function ChannelBlock({
  ch,
  onOpen,
}: {
  ch: AcquisitionChannel;
  onOpen: () => void;
}) {
  const started = ch.stages.find((s) => s.stage === "started")?.count ?? ch.total;
  const isAds = ch.channel.endsWith("_ads");

  return (
    <button
      type="button"
      onClick={onOpen}
      disabled={ch.total === 0}
      className="w-full rounded-lg border border-hairline p-3 text-left transition-colors hover:bg-page disabled:cursor-default disabled:opacity-60"
      title={ch.total > 0 ? `Click to see ${ch.total} prospect${ch.total !== 1 ? "s" : ""}` : undefined}
    >
      <div className="mb-2.5 flex items-center gap-2">
        <span
          className="inline-block h-2 w-2 shrink-0 rounded-full"
          style={{ background: isAds ? "var(--accent)" : "var(--baseline)" }}
          aria-hidden="true"
        />
        <span className="text-sm font-semibold text-ink">{ch.label}</span>
        {isAds ? (
          <span
            className="rounded-full px-1.5 py-px text-[10px] font-medium"
            style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
          >
            paid
          </span>
        ) : null}
        <span className="ml-auto text-sm font-semibold text-ink">
          {fmtNumber(ch.total)}
        </span>
      </div>

      {/* Compact stepper: one cell per funnel stage, connected → onboarded. */}
      <div className="flex items-stretch gap-1.5">
        {ch.stages.map((s, i) => {
          const pct = started > 0 ? s.count / started : null;
          return (
            <div key={s.stage} className="min-w-0 flex-1">
              <div
                className="mb-1 h-1.5 rounded-full"
                style={{
                  background: s.count > 0 ? RAMP[i] : "var(--gridline)",
                }}
                aria-hidden="true"
              />
              <div className="truncate text-[11px] text-muted" title={s.label}>
                {s.label}
              </div>
              <div className="text-xs font-medium tabular-nums text-ink-2">
                {fmtNumber(s.count)}
                {pct !== null && i > 0 ? (
                  <span className="ml-1 text-[10px] text-muted">{fmtPercent(pct)}</span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </button>
  );
}

export function AcquisitionCard({
  channels,
}: {
  channels: AcquisitionChannel[];
}) {
  const [drill, setDrill] = useState<AcquisitionChannel | null>(null);

  return (
    <section className="card p-4">
      <div className="mb-3">
        <h2 className="text-sm font-semibold text-ink">Acquisition by channel</h2>
        <p className="text-xs text-muted">
          Onboarding prospects grouped by where they came from — started chatting
          through fully onboarded. Click a channel to see the prospects.
        </p>
      </div>

      {channels.length === 0 ? (
        <div className="rounded-lg border border-dashed border-hairline p-6 text-center text-xs text-muted">
          No prospects recorded in this date range.
        </div>
      ) : (
        <div className="grid gap-2.5 sm:grid-cols-2">
          {channels.map((ch) => (
            <ChannelBlock key={ch.channel} ch={ch} onOpen={() => setDrill(ch)} />
          ))}
        </div>
      )}

      {drill ? (
        <FunnelDrillModal
          label={`${drill.label} — prospects`}
          sessions={drill.sessions}
          onClose={() => setDrill(null)}
        />
      ) : null}
    </section>
  );
}
