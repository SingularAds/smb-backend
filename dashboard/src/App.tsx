import { useCallback, useEffect, useState } from "react";
import {
  HashRouter,
  NavLink,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router-dom";
import {
  ApiError,
  clearAdminKey,
  fetchBusinessDetail,
  fetchOverview,
  getAdminKey,
} from "./api";
import type {
  BusinessDetail,
  GlobalNumberOption,
  Overview,
  RangeFilter,
} from "./types";
import { ALL_TIME } from "./types";
import { AdminKeyGate } from "./components/AdminKeyGate";
import { FilterBar } from "./components/FilterBar";
import { DetailSkeleton, OverviewSkeleton } from "./components/Skeleton";
import { OverviewPage } from "./pages/Overview";
import { BusinessDetailPage } from "./pages/BusinessDetail";
import { GlobalKbPage } from "./pages/GlobalKb";

// App shell. The filter row lives here (one row above everything it scopes —
// both screens re-render against the same slice). Refetch holds the previous
// render at reduced opacity: no skeletons, no layout jump.

interface FetchState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Session-lifetime stale-while-revalidate cache: revisiting a view (or
// flipping back to a range already seen) paints the last response instantly
// while a fresh one loads in the background. Freshness is the backend's job
// (it keeps its own 60s cache); this only removes blank waits.
const swrCache = new Map<string, unknown>();
const SWR_MAX_ENTRIES = 30;

function swrGet<T>(key: string): T | null {
  return (swrCache.get(key) as T | undefined) ?? null;
}

function swrSet(key: string, value: unknown): void {
  swrCache.delete(key); // refresh insertion order (LRU-ish)
  if (swrCache.size >= SWR_MAX_ENTRIES) {
    const oldest = swrCache.keys().next().value;
    if (oldest !== undefined) swrCache.delete(oldest);
  }
  swrCache.set(key, value);
}

/** Serialize the range filter into a stable cache-key fragment.
 *  A null preset with no `from` means ALL TIME (the funnel's default) and must
 *  NOT collapse to "d30", or the two would share one cache entry. */
function filterKey(f: RangeFilter): string {
  if (f.from) return `${f.from}~${f.to ?? "now"}`;
  return f.preset ? `d${f.preset}` : "all";
}

function useFetch<T>(
  fetcher: () => Promise<T>,
  onAuthFail: () => void,
  cacheKey: string,
) {
  const [state, setState] = useState<FetchState<T>>(() => ({
    data: swrGet<T>(cacheKey),
    loading: true,
    error: null,
  }));

  useEffect(() => {
    let cancelled = false;
    // Stale data for THIS key renders immediately (or null → skeleton);
    // a different entity's data never leaks across keys.
    setState({ data: swrGet<T>(cacheKey), loading: true, error: null });
    fetcher().then(
      (data) => {
        swrSet(cacheKey, data);
        if (!cancelled) setState({ data, loading: false, error: null });
      },
      (err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          onAuthFail();
          return;
        }
        setState((prev) => ({
          ...prev,
          loading: false,
          error:
            err instanceof ApiError
              ? `${err.status}: ${err.message}`
              : "Could not reach the backend. Is the API running?",
        }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [fetcher, onAuthFail, cacheKey]);

  return state;
}

function ErrorPanel({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-hairline bg-surface p-6 text-sm text-ink-2"
    >
      <p className="font-medium text-ink">Something went wrong</p>
      <p className="mt-1">{message}</p>
    </div>
  );
}


function OverviewRoute({
  filter,
  includeTest,
  globalDevice,
  funnelRange,
  onFunnelRangeChange,
  onGlobalNumbers,
  onAuthFail,
}: {
  filter: RangeFilter;
  includeTest: boolean;
  globalDevice: string | null;
  funnelRange: RangeFilter;
  onFunnelRangeChange: (f: RangeFilter) => void;
  /** lifts the available numbers up so the shell's filter row can render them */
  onGlobalNumbers: (options: GlobalNumberOption[]) => void;
  onAuthFail: () => void;
}) {
  const fetcher = useCallback(
    () => fetchOverview(filter, includeTest, globalDevice, funnelRange),
    [filter, includeTest, globalDevice, funnelRange],
  );
  const { data, loading, error } = useFetch<Overview>(
    fetcher,
    onAuthFail,
    `ov:${filterKey(filter)}:${includeTest}:${globalDevice ?? "all"}:f${filterKey(funnelRange)}`,
  );

  // The picker's options come from the response; publish them to the shell.
  const numbers = data?.globalNumbers;
  useEffect(() => {
    if (numbers) onGlobalNumbers(numbers);
  }, [numbers, onGlobalNumbers]);

  if (error) return <ErrorPanel message={error} />;
  if (!data) return <OverviewSkeleton />;
  return (
    <div className={loading ? "is-refetching" : ""}>
      <OverviewPage
        data={data}
        filter={filter}
        funnelRange={funnelRange}
        onFunnelRangeChange={onFunnelRangeChange}
      />
    </div>
  );
}

function DetailRoute({
  filter,
  onAuthFail,
}: {
  filter: RangeFilter;
  onAuthFail: () => void;
}) {
  const { businessId } = useParams<{ businessId: string }>();
  const fetcher = useCallback(
    () => fetchBusinessDetail(businessId ?? "", filter),
    [businessId, filter],
  );
  const { data, loading, error } = useFetch<BusinessDetail>(
    fetcher,
    onAuthFail,
    `biz:${businessId}:${filterKey(filter)}`,
  );

  if (error) return <ErrorPanel message={error} />;
  if (!data) return <DetailSkeleton />;
  return (
    <div className={loading ? "is-refetching" : ""}>
      <BusinessDetailPage detail={data} />
    </div>
  );
}

function Shell({ onAuthFail }: { onAuthFail: () => void }) {
  const location = useLocation();
  const isOverview = location.pathname === "/";
  const isKb = location.pathname.startsWith("/global-kb");
  const [filter, setFilter] = useState<RangeFilter>({
    preset: 30,
    from: null,
    to: null,
  });
  const [includeTest, setIncludeTest] = useState(false);
  // Global-number scope (null = all numbers). The options list is published by
  // the overview route, which is the only screen that can be scoped.
  const [globalDevice, setGlobalDevice] = useState<string | null>(null);
  const [globalNumbers, setGlobalNumbers] = useState<GlobalNumberOption[]>([]);
  // The onboarding funnel has its OWN window, defaulting to all time — it is
  // deliberately not tied to the page's date filter.
  const [funnelRange, setFunnelRange] = useState<RangeFilter>(ALL_TIME);
  const handleGlobalNumbers = useCallback((options: GlobalNumberOption[]) => {
    setGlobalNumbers((prev) =>
      JSON.stringify(prev) === JSON.stringify(options) ? prev : options,
    );
  }, []);

  const navLink = ({ isActive }: { isActive: boolean }) =>
    `rounded-md px-2.5 py-1 text-xs font-medium ${
      isActive ? "bg-accent-soft text-accent" : "text-ink-2 hover:text-ink"
    }`;

  return (
    <div className="mx-auto min-h-screen max-w-7xl px-4 py-5">
      <header className="mb-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-ink">Recepte analytics</h1>
          <span className="text-xs text-muted">internal · team only</span>
          <nav aria-label="Sections" className="ml-auto flex items-center gap-1">
            <NavLink to="/" end className={navLink}>
              Overview
            </NavLink>
            <NavLink to="/global-kb" className={navLink}>
              Global KB
            </NavLink>
            <button
              onClick={onAuthFail}
              className="rounded-md px-2.5 py-1 text-xs font-medium text-[var(--status-critical)] hover:bg-[rgba(208,59,59,0.08)] transition-colors"
              title="Logout and clear admin session"
            >
              Logout
            </button>
          </nav>
        </div>
        {!isKb ? (
          <FilterBar
            filter={filter}
            onFilterChange={setFilter}
            includeTest={isOverview ? includeTest : undefined}
            onIncludeTestChange={isOverview ? setIncludeTest : undefined}
            globalNumbers={isOverview ? globalNumbers : undefined}
            globalDevice={isOverview ? globalDevice : undefined}
            onGlobalDeviceChange={isOverview ? setGlobalDevice : undefined}
          />
        ) : null}
      </header>
      <main>
        <Routes>
          <Route
            path="/"
            element={
              <OverviewRoute
                filter={filter}
                includeTest={includeTest}
                globalDevice={globalDevice}
                funnelRange={funnelRange}
                onFunnelRangeChange={setFunnelRange}
                onGlobalNumbers={handleGlobalNumbers}
                onAuthFail={onAuthFail}
              />
            }
          />
          <Route
            path="/business/:businessId"
            element={<DetailRoute filter={filter} onAuthFail={onAuthFail} />}
          />
          <Route path="/global-kb" element={<GlobalKbPage onAuthFail={onAuthFail} />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const [hasKey, setHasKey] = useState(() => getAdminKey() !== null);
  const [showLoginToast, setShowLoginToast] = useState(false);

  const onAuthFail = useCallback(() => {
    clearAdminKey();
    setHasKey(false);
    setShowLoginToast(false);
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setHasKey(true);
    setShowLoginToast(true);
    setTimeout(() => {
      setShowLoginToast(false);
    }, 3000);
  }, []);

  if (!hasKey) {
    return <AdminKeyGate onSubmitted={handleLoginSuccess} />;
  }

  return (
    <HashRouter>
      {showLoginToast && (
        <div className="fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-lg border border-[var(--status-good)] bg-surface px-4 py-3 text-sm shadow-lg rise border-opacity-30">
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[var(--status-good)] text-white text-[10px] font-bold">✓</span>
          <span className="font-medium text-ink">Logged in successfully</span>
        </div>
      )}
      <Shell onAuthFail={onAuthFail} />
    </HashRouter>
  );
}
