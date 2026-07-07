import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ActivityPoint } from "../types";
import { ChartCard } from "./ChartCard";
import { EmptyState } from "./EmptyState";
import { fmtDate, fmtNumber } from "../lib/format";

// Platform/business activity: TWO series (conversations, bookings) on one
// axis — categorical pair validated light+dark. Two series ⇒ a legend is
// always present (line keys, since the marks are lines); one shared crosshair
// tooltip lists both series at the hovered X.

const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const SERIES = [
  { key: "conversations" as const, label: "Conversations", color: "var(--series-1)" },
  { key: "bookings" as const, label: "Bookings", color: "var(--series-2)" },
];

function LineKey({ color }: { color: string }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-0.5 w-4 rounded-full align-middle"
      style={{ backgroundColor: color }}
    />
  );
}

function ActivityTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ dataKey?: string; value?: number | string; stroke?: string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-hairline bg-surface px-3 py-2 text-xs shadow-md">
      <div className="mb-1 text-muted">{fmtDate(label)}</div>
      {SERIES.map((s) => {
        const row = payload.find((p) => p.dataKey === s.key);
        return (
          <div key={s.key} className="flex items-center gap-2 py-0.5">
            <LineKey color={s.color} />
            {/* values lead, labels follow */}
            <span className="text-sm font-semibold text-ink">
              {fmtNumber(Number(row?.value ?? 0))}
            </span>
            <span className="text-muted">{s.label.toLowerCase()}</span>
          </div>
        );
      })}
    </div>
  );
}

export function ActivityChart({
  points,
  title = "Activity",
  subtitle = "Conversations handled and bookings created per day",
}: {
  points: ActivityPoint[];
  title?: string;
  subtitle?: string;
}) {
  const totals = {
    conversations: points.reduce((a, p) => a + p.conversations, 0),
    bookings: points.reduce((a, p) => a + p.bookings, 0),
  };
  const empty = totals.conversations === 0 && totals.bookings === 0;

  const srSummary = empty
    ? "No conversations or bookings in the selected range."
    : `${totals.conversations} conversations and ${totals.bookings} bookings across the selected range.`;

  const tickInterval = Math.max(0, Math.ceil(points.length / 8) - 1);

  const chart = empty ? (
    <EmptyState
      title="No activity in this range"
      explanation="Conversations handled by the AI and bookings created will chart here per day. Try a wider date range."
    />
  ) : (
    <div>
      {/* Legend — always present for two series; line keys mirror the mark */}
      <div className="mb-2 flex items-center gap-4 text-xs text-ink-2" aria-hidden="true">
        {SERIES.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <LineKey color={s.color} />
            {s.label}
          </span>
        ))}
      </div>
      <div className="h-60">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
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
              content={<ActivityTooltip />}
              cursor={{ stroke: "var(--baseline)", strokeWidth: 1 }}
            />
            {SERIES.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                stroke={s.color}
                strokeWidth={2}
                dot={false}
                isAnimationActive={!REDUCED_MOTION}
                activeDot={{
                  r: 4,
                  fill: s.color,
                  stroke: "var(--surface-1)",
                  strokeWidth: 2,
                }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );

  const nonZero = points.filter((p) => p.conversations > 0 || p.bookings > 0);
  const table = (
    <table className="w-full text-sm">
      <caption className="sr-only">Daily conversations and bookings</caption>
      <thead>
        <tr className="border-b border-hairline text-left text-xs text-muted">
          <th scope="col" className="py-1.5 pr-4 font-medium">Date</th>
          <th scope="col" className="py-1.5 pr-4 text-right font-medium">Conversations</th>
          <th scope="col" className="py-1.5 text-right font-medium">Bookings</th>
        </tr>
      </thead>
      <tbody>
        {nonZero.length === 0 ? (
          <tr>
            <td colSpan={3} className="py-3 text-center text-muted">No activity in this range</td>
          </tr>
        ) : (
          nonZero.map((p) => (
            <tr key={p.date} className="border-b border-hairline last:border-0">
              <td className="py-1.5 pr-4 text-ink-2">{fmtDate(p.date)}</td>
              <td className="py-1.5 pr-4 text-right tabular-nums text-ink">{fmtNumber(p.conversations)}</td>
              <td className="py-1.5 text-right tabular-nums text-ink">{fmtNumber(p.bookings)}</td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );

  return (
    <ChartCard
      title={title}
      subtitle={subtitle}
      srSummary={srSummary}
      chart={chart}
      table={table}
    />
  );
}
