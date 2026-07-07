import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Account, Overview, RangeFilter } from "../types";
import { ActivityChart } from "../components/ActivityChart";
import { FunnelChart } from "../components/FunnelChart";
import { GrowthChart } from "../components/GrowthChart";
import { KpiRow, StatTile } from "../components/StatTile";
import { SortableTable, type Column } from "../components/SortableTable";
import { Badge, PairedBadge, PlanBadge } from "../components/Badge";
import {
  AlertTriangleIcon,
  BuildingIcon,
  CalendarIcon,
  ChatIcon,
  SearchIcon,
  StarIcon,
  TrendingUpIcon,
} from "../components/Icons";
import {
  fmtNumber,
  fmtPercent,
  fmtRelative,
  fmtDate,
  fmtToken,
} from "../lib/format";

// Screen 1 — platform overview. KPI tiles (icon + sparkline) → activity
// (2-series legend chart) → funnel + growth → accounts table with health
// quick-filters. Sections rise in with a small stagger.

type QuickFilter = "all" | "paired" | "trial_expiring" | "complaints";

const QUICK_FILTERS: { id: QuickFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "paired", label: "WhatsApp paired" },
  { id: "trial_expiring", label: "Trial ending ≤ 7d" },
  { id: "complaints", label: "Open complaints" },
];

function matchesQuickFilter(a: Account, f: QuickFilter): boolean {
  switch (f) {
    case "paired":
      return a.whatsappPaired;
    case "trial_expiring":
      return (
        (a.plan === "trial" || a.plan === "trialing") &&
        a.trialDaysLeft !== null &&
        a.trialDaysLeft <= 7
      );
    case "complaints":
      return a.openComplaints > 0;
    default:
      return true;
  }
}

function rise(index: number): React.CSSProperties {
  return { animationDelay: `${index * 70}ms` };
}

export function OverviewPage({
  data,
  filter,
}: {
  data: Overview;
  filter: RangeFilter;
}) {
  const navigate = useNavigate();
  const [quick, setQuick] = useState<QuickFilter>("all");
  const [search, setSearch] = useState("");

  const accounts = useMemo(() => {
    const q = search.trim().toLowerCase();
    return data.accounts.filter(
      (a) =>
        matchesQuickFilter(a, quick) &&
        (q === "" ||
          a.name.toLowerCase().includes(q) ||
          (a.ownerName ?? "").toLowerCase().includes(q) ||
          (a.ownerPhone ?? "").includes(q)),
    );
  }, [data.accounts, quick, search]);

  const agg = data.aggregate;
  const convTrend = data.activity.map((p) => p.conversations);
  const bookTrend = data.activity.map((p) => p.bookings);
  const bizTrend = data.growth.map((p) => p.newBusinesses);

  const columns: Column<Account>[] = [
    {
      key: "name",
      header: "Business",
      sortValue: (a) => a.name.toLowerCase(),
      render: (a) => (
        <div>
          <div className="font-medium text-ink">
            {a.name}
            {a.isTest ? (
              <span className="ml-1.5 rounded bg-grid px-1 py-0.5 text-[10px] font-medium text-muted">
                TEST
              </span>
            ) : null}
          </div>
          <div className="text-xs text-muted">
            {[a.businessType ? fmtToken(a.businessType) : null, a.ownerName]
              .filter(Boolean)
              .join(" · ") || "—"}
          </div>
        </div>
      ),
    },
    {
      key: "plan",
      header: "Plan",
      sortValue: (a) => a.plan ?? "",
      render: (a) => <PlanBadge plan={a.plan} trialDaysLeft={a.trialDaysLeft} />,
    },
    {
      key: "paired",
      header: "WhatsApp",
      sortValue: (a) => (a.whatsappPaired ? 1 : 0),
      render: (a) => <PairedBadge paired={a.whatsappPaired} />,
    },
    {
      key: "bookings",
      header: "Bookings",
      align: "right",
      sortValue: (a) => a.bookingsInRange,
      render: (a) => fmtNumber(a.bookingsInRange),
    },
    {
      key: "conversations",
      header: "Conversations",
      align: "right",
      sortValue: (a) => a.conversationsInRange,
      render: (a) => fmtNumber(a.conversationsInRange),
    },
    {
      key: "complaints",
      header: "Complaints",
      align: "right",
      sortValue: (a) => a.openComplaints,
      render: (a) =>
        a.openComplaints > 0 ? (
          <Badge tone="critical">{a.openComplaints} open</Badge>
        ) : (
          <span className="text-muted">0</span>
        ),
    },
    {
      key: "ownerActivity",
      header: "Owner last active",
      sortValue: (a) => a.lastOwnerActivityAt,
      render: (a) => (
        <span
          className="text-ink-2"
          title="Last message the owner sent on the platform WhatsApp number"
        >
          {fmtRelative(a.lastOwnerActivityAt)}
        </span>
      ),
    },
    {
      key: "customerActivity",
      header: "Customer last active",
      sortValue: (a) => a.lastCustomerActivityAt,
      render: (a) => (
        <span className="text-ink-2">{fmtRelative(a.lastCustomerActivityAt)}</span>
      ),
    },
    {
      key: "created",
      header: "Created",
      sortValue: (a) => a.createdAt,
      render: (a) => <span className="text-ink-2">{fmtDate(a.createdAt)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <div className="rise" style={rise(0)}>
        <KpiRow>
          <StatTile
            label="Businesses"
            value={fmtNumber(agg.totalBusinesses)}
            hint={`${fmtNumber(agg.newBusinessesInRange)} new in range`}
            icon={<BuildingIcon />}
            trend={bizTrend}
          />
          <StatTile
            label="Conversations"
            value={fmtNumber(agg.totalConversations)}
            hint="voice + WhatsApp, in range"
            icon={<ChatIcon />}
            trend={convTrend}
          />
          <StatTile
            label="Bookings"
            value={fmtNumber(agg.totalBookings)}
            hint="created in range"
            icon={<CalendarIcon />}
            trend={bookTrend}
          />
          <StatTile
            label="Booking conversion"
            value={fmtPercent(agg.bookingConversion)}
            hint="booked ÷ conversations with an outcome"
            icon={<TrendingUpIcon />}
          />
          <StatTile
            label="Average CSAT"
            value={agg.avgCsat !== null ? `${agg.avgCsat.toFixed(2)} / 5` : "—"}
            hint={
              agg.csatResponses > 0
                ? `${fmtNumber(agg.csatResponses)} responses`
                : "no ratings yet"
            }
            icon={<StarIcon />}
          />
          <StatTile
            label="Needs attention"
            value={fmtNumber(agg.openComplaints + agg.pendingKnowledgeGaps)}
            hint={`${agg.openComplaints} complaints · ${agg.pendingKnowledgeGaps} KB gaps`}
            icon={<AlertTriangleIcon />}
          />
        </KpiRow>
      </div>

      <div className="rise" style={rise(1)}>
        <ActivityChart
          points={data.activity}
          title="Platform activity"
          subtitle="Conversations handled and bookings created per day, all visible accounts"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rise" style={rise(2)}>
          <FunnelChart
            stages={data.funnel}
            excludedDemoSessions={data.excludedDemoSessions}
          />
        </div>
        <div className="rise" style={rise(3)}>
          <GrowthChart points={data.growth} />
        </div>
      </div>

      <section className="card rise p-4" style={rise(4)}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="mr-2 text-sm font-semibold text-ink">
            Accounts
            <span className="ml-2 font-normal text-muted">
              {accounts.length} of {data.accounts.length}
            </span>
          </h2>
          <div
            role="group"
            aria-label="Account health filters"
            className="flex flex-wrap gap-1.5"
          >
            {QUICK_FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={quick === f.id}
                onClick={() => setQuick(f.id)}
                className={`rounded-full border px-2.5 py-1 text-xs font-medium ${
                  quick === f.id
                    ? "border-accent bg-accent text-white"
                    : "border-hairline text-ink-2 hover:bg-page"
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <label className="relative ml-auto block">
            <span className="sr-only">Search accounts</span>
            <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted">
              <SearchIcon />
            </span>
            <input
              type="search"
              placeholder="Search name, owner, phone…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-56 rounded-md border border-hairline bg-page py-1.5 pl-7 pr-2 text-xs text-ink"
            />
          </label>
        </div>
        <SortableTable
          caption={`Accounts table, ${accounts.length} businesses. Activity columns are scoped to ${filter.from ? "the custom range" : `the last ${filter.preset ?? 30} days`}. Click a row to open the business detail.`}
          columns={columns}
          rows={accounts}
          rowKey={(a) => a.id}
          onRowClick={(a) => navigate(`/business/${encodeURIComponent(a.id)}`)}
          initialSort={{ key: "created", direction: "desc" }}
          emptyMessage="No accounts match the current filters"
        />
      </section>
    </div>
  );
}
