import { Sparkline } from "./Sparkline";

// Stat tile per the dataviz contract: label (sentence case, no colon) with an
// optional muted icon, value (semibold, proportional figures — no tabular-nums
// at display size), optional hint line, optional 12-point sparkline in the
// de-emphasis hue with the current period marked in the accent.

export function StatTile({
  label,
  value,
  hint,
  icon,
  trend,
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: React.ReactNode;
  trend?: number[];
}) {
  return (
    <div className="card px-4 py-3">
      <div className="flex items-center gap-1.5 text-xs text-ink-2">
        {icon ? <span className="text-muted">{icon}</span> : null}
        {label}
      </div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-2xl font-semibold leading-tight text-ink">{value}</div>
        {trend && trend.length > 1 ? <Sparkline values={trend} /> : null}
      </div>
      {hint ? <div className="mt-0.5 text-xs text-muted">{hint}</div> : null}
    </div>
  );
}

export function KpiRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {children}
    </div>
  );
}
