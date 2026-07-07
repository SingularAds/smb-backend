import { useMemo, useState } from "react";
import { SortIcon } from "./Icons";

// Generic sortable table with proper aria-sort semantics. Column headers are
// real <button>s (keyboard-operable); the active sort is announced via
// aria-sort on the <th>.

export interface Column<T> {
  key: string;
  header: string;
  sortValue?: (row: T) => string | number | null;
  render: (row: T) => React.ReactNode;
  align?: "left" | "right";
}

export function SortableTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  initialSort,
  caption,
  emptyMessage = "No rows",
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  initialSort?: { key: string; direction: "asc" | "desc" };
  caption: string;
  emptyMessage?: string;
}) {
  const [sort, setSort] = useState<{ key: string; direction: "asc" | "desc" } | null>(
    initialSort ?? null,
  );

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const dir = sort.direction === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      // Nulls always sort last, in both directions
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (va < vb) return -dir;
      if (va > vb) return dir;
      return 0;
    });
  }, [rows, sort, columns]);

  function toggleSort(key: string) {
    setSort((prev) =>
      prev?.key === key
        ? { key, direction: prev.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "desc" },
    );
  }

  return (
    // ~5 rows visible, then scroll within the card (header stays pinned).
    <div
      className="max-h-[19rem] overflow-auto overscroll-contain"
      tabIndex={0}
      role="group"
      aria-label={caption}
    >
      <table className="w-full text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            {columns.map((col) => {
              const active = sort?.key === col.key;
              const ariaSort = active
                ? sort!.direction === "asc"
                  ? "ascending"
                  : "descending"
                : undefined;
              return (
                <th
                  key={col.key}
                  scope="col"
                  aria-sort={ariaSort}
                  className={`sticky top-0 z-10 bg-surface py-2 pr-3 font-medium shadow-[inset_0_-1px_0_var(--gridline)] ${col.align === "right" ? "text-right" : ""}`}
                >
                  {col.sortValue ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(col.key)}
                      className="inline-flex items-center gap-1 hover:text-ink"
                    >
                      {col.header}
                      <SortIcon direction={active ? sort!.direction : null} />
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="py-6 text-center text-muted">
                {emptyMessage}
              </td>
            </tr>
          ) : (
            sorted.map((row) => (
              <tr
                key={rowKey(row)}
                className={`border-b border-hairline last:border-0 ${
                  onRowClick ? "cursor-pointer hover:bg-page" : ""
                }`}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={`py-2 pr-3 ${col.align === "right" ? "text-right tabular-nums" : ""}`}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
