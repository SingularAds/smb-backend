import type { RangeFilter } from "../types";
import { DateRangeDropdown } from "./DateRangeDropdown";

// One filter row above everything it scopes (never per-chart). Date range
// first, via the preset/custom dropdown; the include-test toggle only renders
// on the overview (the drill-down is already a single account).

export function FilterBar({
  filter,
  onFilterChange,
  includeTest,
  onIncludeTestChange,
}: {
  filter: RangeFilter;
  onFilterChange: (f: RangeFilter) => void;
  includeTest?: boolean;
  onIncludeTestChange?: (v: boolean) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3" role="group" aria-label="Filters">
      <DateRangeDropdown filter={filter} onChange={onFilterChange} />
      {onIncludeTestChange ? (
        <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-ink-2">
          <input
            type="checkbox"
            checked={!includeTest}
            onChange={(e) => onIncludeTestChange(!e.target.checked)}
            className="h-3.5 w-3.5 accent-[var(--accent)]"
          />
          Hide test/demo accounts
        </label>
      ) : null}
    </div>
  );
}
