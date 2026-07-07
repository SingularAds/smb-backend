import { useState } from "react";
import type { FunnelStage } from "../types";
import { ChartCard } from "./ChartCard";
import { FunnelDrillModal } from "./FunnelDrillModal";
import { fmtNumber, fmtPercent } from "../lib/format";

// Onboarding registration funnel: 4 ordered stages → horizontal bars on an
// ordinal single-hue ramp (validated --ordinal, light AND dark). Between
// stages the drop-off is shown explicitly ("who is leaving and where").
// Only SMB-owner REGISTRATION sessions are counted — demo/booking-roleplay
// sessions are excluded by the backend.
// Clicking any bar (or table row) opens a FunnelDrillModal listing owners.

const RAMP = [
  "var(--ordinal-1)",
  "var(--ordinal-2)",
  "var(--ordinal-3)",
  "var(--ordinal-4)",
];

export function FunnelChart({
  stages,
  excludedDemoSessions = 0,
}: {
  stages: FunnelStage[];
  excludedDemoSessions?: number;
}) {
  const max = Math.max(1, ...stages.map((s) => s.count));
  const [drillStage, setDrillStage] = useState<FunnelStage | null>(null);

  const srSummary =
    `Onboarding registration funnel: ` +
    stages
      .map((s, i) => {
        let part = `${s.label} ${s.count}`;
        if (s.pctOfStarted !== null && i > 0) {
          part += ` (${fmtPercent(s.pctOfStarted)} of started)`;
        }
        if (s.dropped > 0) {
          part += `, ${s.dropped} dropped before this stage`;
        }
        return part;
      })
      .join("; ") +
    (excludedDemoSessions > 0
      ? `. ${excludedDemoSessions} demo sessions excluded.`
      : ".");

  const chart = (
    <div role="list">
      {stages.map((s, i) => (
        <div key={s.stage} role="listitem">
          {/* Drop-off connector between stages */}
          {i > 0 ? (
            <div className="flex items-center gap-2 py-1 pl-1 text-[11px] text-muted">
              <span aria-hidden="true" className="inline-block h-3 w-px bg-[var(--baseline)]" />
              {s.dropped > 0 ? (
                <span>
                  −{fmtNumber(s.dropped)} dropped here
                  {s.dropPct !== null ? ` (${fmtPercent(s.dropPct)} of previous stage)` : ""}
                </span>
              ) : (
                <span>no drop-off</span>
              )}
            </div>
          ) : null}

          {/* Clickable bar row — opens drill-down modal */}
          <button
            type="button"
            onClick={() => s.count > 0 && setDrillStage(s)}
            disabled={s.count === 0}
            className="group w-full rounded-md px-1 py-0.5 text-left transition-colors hover:bg-[var(--page)] disabled:cursor-default disabled:opacity-60"
            title={s.count > 0 ? `Click to see ${s.count} owners at this stage` : undefined}
          >
            <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
              <span className="text-ink-2 transition-colors group-hover:text-ink">
                {s.label}
              </span>
              <span className="text-muted">
                {s.pctOfStarted !== null
                  ? i === 0
                    ? `${fmtNumber(s.count)} started`
                    : `${fmtPercent(s.pctOfStarted)} of started`
                  : " "}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-6 flex-1 rounded-r" title={`${s.label}: ${s.count}`}>
                <div
                  className="h-6 rounded-r transition-[width] duration-500"
                  style={{
                    width: `${Math.max((s.count / max) * 100, s.count > 0 ? 2 : 0)}%`,
                    backgroundColor: RAMP[i] ?? RAMP[RAMP.length - 1],
                    minWidth: s.count > 0 ? "8px" : "0",
                  }}
                />
              </div>
              {/* value at the bar tip */}
              <span className="w-10 text-right text-sm font-semibold text-ink">
                {fmtNumber(s.count)}
              </span>
            </div>
          </button>
        </div>
      ))}
    </div>
  );

  const table = (
    <table className="w-full text-sm">
      <caption className="sr-only">Onboarding registration funnel data</caption>
      <thead>
        <tr className="border-b border-hairline text-left text-xs text-muted">
          <th scope="col" className="py-1.5 pr-4 font-medium">Stage</th>
          <th scope="col" className="py-1.5 pr-4 text-right font-medium">Sessions</th>
          <th scope="col" className="py-1.5 pr-4 text-right font-medium">% of started</th>
          <th scope="col" className="py-1.5 text-right font-medium">Dropped before stage</th>
        </tr>
      </thead>
      <tbody>
        {stages.map((s) => (
          <tr
            key={s.stage}
            onClick={() => s.count > 0 && setDrillStage(s)}
            className={`border-b border-hairline last:border-0 ${
              s.count > 0 ? "cursor-pointer hover:bg-[var(--page)]" : ""
            }`}
            title={s.count > 0 ? `Click to see ${s.count} owners` : undefined}
          >
            <td className="py-1.5 pr-4 text-ink-2">{s.label}</td>
            <td className="py-1.5 pr-4 text-right tabular-nums text-ink">
              {fmtNumber(s.count)}
            </td>
            <td className="py-1.5 pr-4 text-right tabular-nums text-ink-2">
              {s.pctOfStarted !== null ? fmtPercent(s.pctOfStarted) : "—"}
            </td>
            <td className="py-1.5 text-right tabular-nums text-ink-2">
              {s.dropped > 0
                ? `${fmtNumber(s.dropped)}${s.dropPct !== null ? ` (${fmtPercent(s.dropPct)})` : ""}`
                : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <>
      <ChartCard
        title="Onboarding funnel"
        subtitle={`Owner registration sessions started in range — demo/booking roleplay excluded${
          excludedDemoSessions > 0 ? ` (${excludedDemoSessions} in range)` : ""
        } · test accounts excluded · click a bar to see owners`}
        srSummary={srSummary}
        chart={chart}
        table={table}
      />

      {drillStage ? (
        <FunnelDrillModal
          label={drillStage.label}
          sessions={drillStage.sessions}
          onClose={() => setDrillStage(null)}
        />
      ) : null}
    </>
  );
}
