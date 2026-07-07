import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { GrowthPoint } from "../types";
import { ChartCard } from "./ChartCard";
import { fmtDate, fmtNumber } from "../lib/format";
import { EmptyState } from "./EmptyState";

// Growth trend: single series → columns in series-1, ≤24px thick, 4px rounded
// cap, square at the baseline. One series ⇒ no legend box (the title names
// it). Columns keep it visually distinct from the multi-series line chart.

const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function GrowthTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ value?: number | string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-hairline bg-surface px-3 py-2 text-xs shadow-md">
      <div className="text-sm font-semibold text-ink">
        {fmtNumber(Number(payload[0]?.value ?? 0))} new
      </div>
      <div className="text-muted">{fmtDate(label)}</div>
    </div>
  );
}

export function GrowthChart({ points }: { points: GrowthPoint[] }) {
  const total = points.reduce((acc, p) => acc + p.newBusinesses, 0);
  const peak = points.reduce(
    (best, p) => (p.newBusinesses > best.newBusinesses ? p : best),
    { date: "", newBusinesses: 0 },
  );

  const srSummary =
    total === 0
      ? "No new businesses were created in the selected range."
      : `${total} new businesses in the selected range; busiest day ${fmtDate(peak.date)} with ${peak.newBusinesses}.`;

  const tickInterval = Math.max(0, Math.ceil(points.length / 8) - 1);

  const chart =
    total === 0 ? (
      <EmptyState
        title="No new businesses in this range"
        explanation="Businesses appear here the moment onboarding completes. Try a wider date range, or include test accounts."
      />
    ) : (
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="var(--gridline)" strokeWidth={1} vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={(d: string) => fmtDate(d)}
              interval={tickInterval}
              tick={{ fill: "var(--text-muted)", fontSize: 11 }}
              axisLine={{ stroke: "var(--baseline)" }}
              tickLine={false}
            />
            <YAxis
              allowDecimals={false}
              tick={{ fill: "var(--text-muted)", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              content={<GrowthTooltip />}
              cursor={{ fill: "var(--accent-soft)" }}
            />
            <Bar
              dataKey="newBusinesses"
              fill="var(--series-1)"
              maxBarSize={24}
              radius={[4, 4, 0, 0]}
              isAnimationActive={!REDUCED_MOTION}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    );

  const nonZero = points.filter((p) => p.newBusinesses > 0);
  const table = (
    <table className="w-full text-sm">
      <caption className="sr-only">New businesses per day</caption>
      <thead>
        <tr className="border-b border-hairline text-left text-xs text-muted">
          <th scope="col" className="py-1.5 pr-4 font-medium">Date</th>
          <th scope="col" className="py-1.5 text-right font-medium">New businesses</th>
        </tr>
      </thead>
      <tbody>
        {nonZero.length === 0 ? (
          <tr>
            <td colSpan={2} className="py-3 text-center text-muted">
              No new businesses in this range
            </td>
          </tr>
        ) : (
          nonZero.map((p) => (
            <tr key={p.date} className="border-b border-hairline last:border-0">
              <td className="py-1.5 pr-4 text-ink-2">{fmtDate(p.date)}</td>
              <td className="py-1.5 text-right tabular-nums text-ink">
                {fmtNumber(p.newBusinesses)}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );

  return (
    <ChartCard
      title="New businesses over time"
      subtitle="From businesses.createdAt"
      srSummary={srSummary}
      chart={chart}
      table={table}
    />
  );
}
