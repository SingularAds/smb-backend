import { useId, useState } from "react";
import { ChartIcon, TableIcon } from "./Icons";

// Card wrapper for every chart. Provides:
//  - a visible title + optional subtitle
//  - a screen-reader summary (aria-describedby on the figure)
//  - a chart ⇄ table toggle: every chart ships its WCAG-clean table twin,
//    so no value is reachable only via hover/color.

export function ChartCard({
  title,
  subtitle,
  srSummary,
  chart,
  table,
}: {
  title: string;
  subtitle?: string;
  srSummary: string;
  chart: React.ReactNode;
  table: React.ReactNode;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const summaryId = useId();
  return (
    <section className="card p-4 h-full flex flex-col">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          {subtitle ? <p className="text-xs text-muted">{subtitle}</p> : null}
        </div>
        <button
          type="button"
          onClick={() => setView(view === "chart" ? "table" : "chart")}
          className="inline-flex items-center gap-1 rounded border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-page"
          aria-pressed={view === "table"}
        >
          {view === "chart" ? <TableIcon /> : <ChartIcon />}
          {view === "chart" ? "View data" : "View chart"}
        </button>
      </div>
      <p id={summaryId} className="sr-only">
        {srSummary}
      </p>
      {view === "chart" ? (
        <figure role="img" aria-describedby={summaryId} aria-label={title} className="flex-1 flex flex-col justify-end">
          {chart}
        </figure>
      ) : (
        <div
          className="max-h-64 flex-1 overflow-auto overscroll-contain"
          tabIndex={0}
          role="group"
          aria-label={`${title} data table`}
        >
          {table}
        </div>
      )}
    </section>
  );
}
