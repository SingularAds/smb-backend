import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CsatPoint } from "../types";
import { ChartCard } from "./ChartCard";
import { fmtDateTime } from "../lib/format";
import { EmptyState } from "./EmptyState";

// CSAT trend: single series (ratings over time, 1–5 fixed domain).
// Verified reality: production has ZERO ratings today, so the empty state is
// the primary render — it explains the pipeline instead of showing a blank plot.

const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function CsatTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload?: CsatPoint }>;
}) {
  const p = payload?.[0]?.payload;
  if (!active || !p) return null;
  return (
    <div className="rounded border border-hairline bg-surface px-2.5 py-1.5 text-xs shadow-sm">
      <div className="text-sm font-semibold text-ink">{p.rating} / 5</div>
      <div className="text-muted">{fmtDateTime(p.at)}</div>
    </div>
  );
}

export function CsatTrendChart({ points }: { points: CsatPoint[] }) {
  const avg =
    points.length > 0
      ? points.reduce((a, p) => a + p.rating, 0) / points.length
      : null;

  const srSummary =
    points.length === 0
      ? "No CSAT ratings received in the selected range."
      : `${points.length} CSAT ratings in range, averaging ${avg?.toFixed(2)} out of 5.`;

  const chart =
    points.length === 0 ? (
      <EmptyState
        title="No CSAT ratings yet"
        explanation="Ratings appear after customers reply to the automated 1–5 satisfaction prompt sent when a WhatsApp conversation goes quiet. The CSAT pipeline is live; no customer has responded in this range."
      />
    ) : (
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={points}
            margin={{ top: 8, right: 8, bottom: 0, left: -24 }}
          >
            <CartesianGrid stroke="var(--gridline)" strokeWidth={1} vertical={false} />
            <XAxis
              dataKey="at"
              tickFormatter={(v: string) => fmtDateTime(v)}
              tick={{ fill: "var(--text-muted)", fontSize: 11 }}
              axisLine={{ stroke: "var(--baseline)" }}
              tickLine={false}
            />
            <YAxis
              domain={[1, 5]}
              ticks={[1, 2, 3, 4, 5]}
              tick={{ fill: "var(--text-muted)", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              content={<CsatTooltip />}
              cursor={{ stroke: "var(--baseline)", strokeWidth: 1 }}
            />
            <Line
              type="monotone"
              dataKey="rating"
              stroke="var(--series-1)"
              strokeWidth={2}
              isAnimationActive={!REDUCED_MOTION}
              dot={{
                r: 4,
                fill: "var(--series-1)",
                stroke: "var(--surface-1)",
                strokeWidth: 2,
              }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    );

  const table = (
    <table className="w-full text-sm">
      <caption className="sr-only">CSAT ratings in range</caption>
      <thead>
        <tr className="border-b border-hairline text-left text-xs text-muted">
          <th scope="col" className="py-1.5 pr-4 font-medium">When</th>
          <th scope="col" className="py-1.5 pr-4 font-medium">Customer</th>
          <th scope="col" className="py-1.5 text-right font-medium">Rating</th>
        </tr>
      </thead>
      <tbody>
        {points.length === 0 ? (
          <tr>
            <td colSpan={3} className="py-3 text-center text-muted">
              No ratings in this range
            </td>
          </tr>
        ) : (
          points.map((p, i) => (
            <tr key={`${p.at}-${i}`} className="border-b border-hairline last:border-0">
              <td className="py-1.5 pr-4 text-ink-2">{fmtDateTime(p.at)}</td>
              <td className="py-1.5 pr-4 text-ink-2">{p.customerPhone ?? "—"}</td>
              <td className="py-1.5 text-right tabular-nums text-ink">{p.rating} / 5</td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );

  return (
    <ChartCard
      title="CSAT trend"
      subtitle="WhatsApp 1–5 post-conversation ratings"
      srSummary={srSummary}
      chart={chart}
      table={table}
    />
  );
}
