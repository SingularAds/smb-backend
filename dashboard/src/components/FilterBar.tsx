import type { GlobalNumberOption, RangeFilter } from "../types";
import { DateRangeDropdown } from "./DateRangeDropdown";
import { GlobalNumberDropdown } from "./GlobalNumberDropdown";

// One filter row above everything it scopes (never per-chart). Date range
// first, via the preset/custom dropdown; the global-number picker and the
// include-test toggle only render on the overview (the drill-down is already a
// single account, and its number is shown in the header).

export function FilterBar({
  filter,
  onFilterChange,
  includeTest,
  onIncludeTestChange,
  globalNumbers,
  globalDevice,
  onGlobalDeviceChange,
}: {
  filter: RangeFilter;
  onFilterChange: (f: RangeFilter) => void;
  includeTest?: boolean;
  onIncludeTestChange?: (v: boolean) => void;
  globalNumbers?: GlobalNumberOption[];
  globalDevice?: string | null;
  onGlobalDeviceChange?: (deviceId: string | null) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3" role="group" aria-label="Filters">
      <DateRangeDropdown filter={filter} onChange={onFilterChange} />
      {onGlobalDeviceChange && globalNumbers && globalNumbers.length > 0 ? (
        <GlobalNumberDropdown
          options={globalNumbers}
          value={globalDevice ?? null}
          onChange={onGlobalDeviceChange}
        />
      ) : null}
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
