// Skeleton loading states — shown on FIRST load only (nothing to hold on
// screen yet). Refetches keep the previous render at reduced opacity instead
// (see App.tsx), so there is never a skeleton flash on filter changes.

function Sk({ className }: { className: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />;
}

function KpiSkeletonRow() {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="card px-4 py-3">
          <Sk className="h-3 w-20" />
          <Sk className="mt-2 h-7 w-14" />
          <Sk className="mt-2 h-2.5 w-24" />
        </div>
      ))}
    </div>
  );
}

export function OverviewSkeleton() {
  return (
    <div role="status" aria-label="Loading dashboard" className="space-y-4">
      <span className="sr-only">Loading dashboard…</span>
      <KpiSkeletonRow />
      <div className="card p-4">
        <Sk className="h-4 w-40" />
        <Sk className="mt-3 h-56 w-full" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="card p-4">
            <Sk className="h-4 w-44" />
            <Sk className="mt-3 h-48 w-full" />
          </div>
        ))}
      </div>
      <div className="card p-4">
        <Sk className="h-4 w-32" />
        <div className="mt-3 space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Sk key={i} className="h-9 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}

export function DetailSkeleton() {
  return (
    <div role="status" aria-label="Loading business detail" className="space-y-4">
      <span className="sr-only">Loading business detail…</span>
      <Sk className="h-4 w-24" />
      <div className="card p-4">
        <Sk className="h-6 w-56" />
        <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i}>
              <Sk className="h-2.5 w-16" />
              <Sk className="mt-1.5 h-4 w-24" />
            </div>
          ))}
        </div>
      </div>
      <KpiSkeletonRow />
      <div className="card p-4">
        <Sk className="h-4 w-32" />
        <Sk className="mt-3 h-48 w-full" />
      </div>
      <div className="card p-4">
        <Sk className="h-4 w-28" />
        <div className="mt-3 space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Sk key={i} className="h-9 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}
