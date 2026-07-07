import { InboxIcon } from "./Icons";

// Empty states are first-class: CSAT and knowledge gaps are verified-empty in
// production today, so these render often. Always explain WHY it's empty and
// what will make data appear — never a blank chart.

export function EmptyState({
  title,
  explanation,
}: {
  title: string;
  explanation: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-hairline px-6 py-10 text-center">
      <span className="text-muted">
        <InboxIcon className="!h-6 !w-6" />
      </span>
      <p className="text-sm font-medium text-ink-2">{title}</p>
      <p className="max-w-md text-xs text-muted">{explanation}</p>
    </div>
  );
}
