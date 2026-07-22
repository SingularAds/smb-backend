import { useState } from "react";
import { Link } from "react-router-dom";
import type { Booking, BusinessDetail } from "../types";
import { ActivityChart } from "../components/ActivityChart";
import { BookingModal } from "../components/BookingModal";
import { CsatTrendChart } from "../components/CsatTrendChart";
import { ConversationsTable } from "../components/ConversationsTable";
import { EmptyState } from "../components/EmptyState";
import { OnboardingChatCard } from "../components/OnboardingChatCard";
import { KpiRow, StatTile } from "../components/StatTile";
import { SortableTable, type Column } from "../components/SortableTable";
import {
  Badge,
  BookingStatusBadge,
  ComplaintStatusBadge,
  KbStatusBadge,
  PairedBadge,
  PlanBadge,
} from "../components/Badge";
import {
  AlertTriangleIcon,
  CalendarIcon,
  ChatIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  InboxIcon,
  StarIcon,
  TrendingUpIcon,
} from "../components/Icons";
import {
  fmtDate,
  fmtDateTime,
  fmtNumber,
  fmtPercent,
  fmtPhone,
  fmtRelative,
  fmtToken,
} from "../lib/format";

// Screen 2 — single-business drill-down: profile header, KPI row, activity,
// CSAT trend, bookings (row click → detail modal), conversations (row click →
// WhatsApp-style chat modal), complaints, knowledge gaps.

function rise(index: number): React.CSSProperties {
  return { animationDelay: `${index * 70}ms` };
}

function HeaderFact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink-2">{value}</dd>
    </div>
  );
}

function ServicesAndHours({ detail }: { detail: BusinessDetail }) {
  const [open, setOpen] = useState(false);
  const { services, hours, openingDays } = detail.profile;
  const hasAny =
    services.length > 0 || (hours?.length ?? 0) > 0 || (openingDays?.length ?? 0) > 0;
  if (!hasAny) return null;
  return (
    <div className="border-t border-hairline pt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-xs font-medium text-ink-2 hover:text-ink"
      >
        <span className={open ? "rotate-180" : ""}>
          <ChevronDownIcon />
        </span>
        Services &amp; hours
      </button>
      {open ? (
        <div className="mt-2 grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <h3 className="mb-1 text-xs font-medium text-muted">
              Services ({services.length})
            </h3>
            {services.length === 0 ? (
              <p className="text-xs text-muted">No services on file.</p>
            ) : (
              <ul className="space-y-0.5">
                {services.map((s, i) => (
                  <li key={i} className="text-ink-2">
                    {s.name ?? "—"}
                    <span className="text-muted">
                      {s.duration ? ` · ${s.duration} min` : ""}
                      {s.price !== null && s.price !== undefined && s.price !== 0
                        ? ` · ${s.price}`
                        : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <h3 className="mb-1 text-xs font-medium text-muted">Hours</h3>
            {hours && hours.length > 0 ? (
              <ul className="space-y-0.5">
                {hours.map((h, i) => (
                  <li key={i} className="text-ink-2">
                    {h}
                  </li>
                ))}
              </ul>
            ) : openingDays && openingDays.length > 0 ? (
              <p className="text-ink-2">Open: {openingDays.join(", ")}</p>
            ) : (
              <p className="text-xs text-muted">No opening hours on file.</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function BusinessDetailPage({ detail }: { detail: BusinessDetail }) {
  const p = detail.profile;
  const s = detail.summary;
  const [openBooking, setOpenBooking] = useState<Booking | null>(null);

  const bookingColumns: Column<Booking>[] = [
    {
      key: "datetime",
      header: "When",
      sortValue: (b) => b.datetime ?? b.createdAt,
      render: (b) => (
        <span className="whitespace-nowrap text-ink-2">{fmtDateTime(b.datetime)}</span>
      ),
    },
    {
      key: "customer",
      header: "Customer",
      sortValue: (b) => (b.customerName ?? b.customerPhone ?? "").toLowerCase(),
      render: (b) => (
        <div>
          <div className="text-ink">{b.customerName ?? "—"}</div>
          <div className="text-xs text-muted">
            {b.customerPhone ? fmtPhone(b.customerPhone) : ""}
          </div>
        </div>
      ),
    },
    {
      key: "service",
      header: "Service",
      sortValue: (b) => b.service ?? "",
      render: (b) => <span className="text-ink-2">{b.service ?? "—"}</span>,
    },
    {
      key: "party",
      header: "Party",
      align: "right",
      sortValue: (b) => b.partySize,
      render: (b) => fmtNumber(b.partySize),
    },
    {
      key: "status",
      header: "Status",
      sortValue: (b) => b.status,
      render: (b) => <BookingStatusBadge status={b.status} />,
    },
    {
      key: "source",
      header: "Source",
      sortValue: (b) => b.source ?? "",
      render: (b) => (
        <span className="text-ink-2">{b.source ? fmtToken(b.source) : "—"}</span>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-xs font-medium text-ink-2 hover:text-ink"
      >
        <ChevronLeftIcon />
        All accounts
      </Link>

      {/* Profile header */}
      <section className="card rise p-4" style={rise(0)}>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold text-ink">{p.name}</h1>
          {p.isTest ? (
            <span className="rounded bg-grid px-1.5 py-0.5 text-[10px] font-medium text-muted">
              TEST
            </span>
          ) : null}
          <PlanBadge plan={p.plan} trialDaysLeft={p.trialDaysLeft} />
          <PairedBadge paired={p.whatsappPaired} />
          {p.status && p.status !== "active" ? (
            <Badge tone="warning">{p.status}</Badge>
          ) : null}
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
          <HeaderFact label="Owner" value={p.ownerName ?? "—"} />
          <HeaderFact
            label="Owner phone"
            value={fmtPhone(p.ownerPhone)}
          />
          <HeaderFact
            label="WhatsApp number"
            value={p.waPhoneNumber ? fmtPhone(p.waPhoneNumber) : p.whatsappPaired ? "paired" : "—"}
          />
          <HeaderFact label="Onboarded" value={fmtDate(p.createdAt)} />
          <HeaderFact
            label="Trial ends"
            value={
              p.trialEndsAt
                ? `${fmtDate(p.trialEndsAt)}${p.trialDaysLeft !== null ? ` (${p.trialDaysLeft}d)` : ""}`
                : "—"
            }
          />
          <HeaderFact
            label="Owner last active"
            value={
              <span title="Last message the owner sent on the platform WhatsApp number">
                {fmtRelative(p.lastOwnerActivityAt)}
              </span>
            }
          />
          <HeaderFact
            label="Type"
            value={p.businessType ? fmtToken(p.businessType) : "—"}
          />
          <HeaderFact label="Language" value={p.primaryLanguage ?? "—"} />
          <HeaderFact label="Timezone" value={p.timezone ?? "—"} />
          <HeaderFact
            label="Onboarding step"
            value={p.onboardingStep ? fmtToken(p.onboardingStep) : "—"}
          />
          <HeaderFact
            label="Onboarded via"
            value={
              <span title="The global Recepte number this owner started their onboarding chat on">
                {p.onboardingNumber ? fmtPhone(p.onboardingNumber) : "—"}
              </span>
            }
          />
          <HeaderFact label="Customers" value={fmtNumber(s.totalCustomers)} />
          <HeaderFact label="Address" value={p.address ?? "—"} />
        </dl>
        <div className="mt-3">
          <ServicesAndHours detail={detail} />
        </div>
      </section>

      {/* The owner's own onboarding chat with Sofia on the global number.
          Shown for paired AND unpaired accounts — for an account that stalled
          before pairing this is the only record of what actually happened. */}
      <OnboardingChatCard
        chat={detail.onboardingChat}
        businessName={p.name}
        whatsappPaired={p.whatsappPaired}
        style={rise(1)}
      />

      {/* KPI row (range-scoped) */}
      <div className="rise" style={rise(2)}>
        <KpiRow>
          <StatTile
            label="Bookings"
            value={fmtNumber(s.bookingsInRange)}
            hint="in range"
            icon={<CalendarIcon />}
            trend={detail.activity.map((a) => a.bookings)}
          />
          <StatTile
            label="Conversations"
            value={fmtNumber(s.conversationsInRange)}
            hint="voice + WhatsApp, in range"
            icon={<ChatIcon />}
            trend={detail.activity.map((a) => a.conversations)}
          />
          <StatTile
            label="Booking conversion"
            value={fmtPercent(s.bookingConversion)}
            hint="booked ÷ conversations with an outcome"
            icon={<TrendingUpIcon />}
          />
          <StatTile
            label="Average CSAT"
            value={s.avgCsat !== null ? `${s.avgCsat.toFixed(2)} / 5` : "—"}
            hint={s.csatResponses > 0 ? `${s.csatResponses} responses` : "no ratings yet"}
            icon={<StarIcon />}
          />
          <StatTile
            label="Open complaints"
            value={fmtNumber(s.openComplaints)}
            icon={<AlertTriangleIcon />}
          />
          <StatTile
            label="Pending KB gaps"
            value={fmtNumber(s.pendingKnowledgeGaps)}
            icon={<InboxIcon />}
          />
        </KpiRow>
      </div>

      <div className="rise" style={rise(3)}>
        <ActivityChart
          points={detail.activity}
          title="Activity"
          subtitle="Conversations and bookings per day for this business"
        />
      </div>

      <div className="rise" style={rise(4)}>
        <CsatTrendChart points={detail.csatTrend} />
      </div>

      {/* Bookings */}
      <section className="card rise p-4" style={rise(5)}>
        <h2 className="mb-1 text-sm font-semibold text-ink">
          Bookings
          <span className="ml-2 font-normal text-muted">
            {fmtNumber(s.bookingsInRange)} in range
          </span>
        </h2>
        {s.bookingsTruncated ? (
          <p className="mb-2 text-xs text-muted">
            Showing the {detail.bookings.length} most recent — narrow the date range to
            see the rest.
          </p>
        ) : null}
        {Object.keys(s.bookingsByStatus).length > 0 ? (
          <p className="mb-2 flex flex-wrap gap-1.5">
            {Object.entries(s.bookingsByStatus).map(([status, count]) => (
              <span
                key={status}
                className="inline-flex items-center gap-1 text-xs text-muted"
              >
                <BookingStatusBadge status={status} /> {fmtNumber(count)}
              </span>
            ))}
          </p>
        ) : null}
        {detail.bookings.length === 0 ? (
          <EmptyState
            title="No bookings in this range"
            explanation={
              detail.allTime.bookings > 0
                ? `This business has ${detail.allTime.bookings} bookings in total — the latest on ${fmtDate(detail.allTime.lastBookingAt)}, outside the selected range. Widen the range (e.g. "Last 12 months") to see them.`
                : "Bookings made via WhatsApp, voice calls, or the API will appear here. This business has no bookings yet."
            }
          />
        ) : (
          <>
            <p className="mb-1 text-xs text-muted">Click a booking for full details.</p>
            <SortableTable
              caption="Bookings in the selected range. Click a row for full booking details."
              columns={bookingColumns}
              rows={detail.bookings}
              rowKey={(b) => b.id}
              onRowClick={(b) => setOpenBooking(b)}
              initialSort={{ key: "datetime", direction: "desc" }}
            />
          </>
        )}
      </section>

      {/* Conversations */}
      <section className="card rise p-4" style={rise(6)}>
        <h2 className="mb-1 text-sm font-semibold text-ink">
          Conversations
          <span className="ml-2 font-normal text-muted">
            {fmtNumber(s.conversationsInRange)} in range
          </span>
        </h2>
        {s.conversationsTruncated ? (
          <p className="mb-2 text-xs text-muted">
            Showing the {detail.conversations.length} most recent — narrow the date
            range to see the rest.
          </p>
        ) : null}
        {detail.conversations.length === 0 ? (
          <EmptyState
            title="No conversations in this range"
            explanation={
              detail.allTime.conversations > 0
                ? `This business has ${detail.allTime.conversations} conversations in total — the latest on ${fmtDate(detail.allTime.lastConversationAt)}, outside the selected range. Widen the range (e.g. "Last 12 months") to see them.`
                : "Voice calls and WhatsApp chats handled by the AI receptionist will appear here with their transcripts. This business has none yet."
            }
          />
        ) : (
          <ConversationsTable
            conversations={detail.conversations}
            transcripts={detail.transcripts}
          />
        )}
      </section>

      {/* Complaints + knowledge gaps */}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card rise p-4" style={rise(7)}>
          <h2 className="mb-2 text-sm font-semibold text-ink">
            Complaints
            <span className="ml-2 font-normal text-muted">
              {detail.complaints.length} total
            </span>
          </h2>
          {detail.complaints.length === 0 ? (
            <EmptyState
              title="No complaints"
              explanation="Complaints escalated by the AI (or logged during voice calls) appear here with their resolution status."
            />
          ) : (
            <ul
              className="max-h-[19rem] space-y-2 overflow-y-auto overscroll-contain pr-1"
              tabIndex={0}
              aria-label={`Complaints, ${detail.complaints.length} total`}
            >
              {detail.complaints.map((c) => (
                <li key={c.id} className="rounded-lg border border-hairline p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <ComplaintStatusBadge status={c.status} />
                    {c.type ? <Badge tone="neutral">{fmtToken(c.type)}</Badge> : null}
                    <span className="ml-auto text-xs text-muted">
                      {fmtDateTime(c.createdAt)}
                    </span>
                  </div>
                  <p className="mt-1.5 text-sm text-ink-2">{c.text || "(no text)"}</p>
                  <p className="mt-1 text-xs text-muted">
                    {[c.customerName, c.customerPhone].filter(Boolean).join(" · ") ||
                      "Unknown customer"}
                    {c.bookingId ? ` · booking ${c.bookingId}` : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card rise p-4" style={rise(8)}>
          <h2 className="mb-2 text-sm font-semibold text-ink">
            Knowledge gaps
            <span className="ml-2 font-normal text-muted">
              {Object.entries(detail.knowledgeByStatus)
                .map(([k, v]) => `${v} ${fmtToken(k).toLowerCase()}`)
                .join(" · ") || "0"}
            </span>
          </h2>
          {detail.knowledgeGaps.length === 0 ? (
            <EmptyState
              title="No knowledge gaps"
              explanation="When a customer asks something the AI can't answer, the question is captured here and the owner is asked (on WhatsApp) whether to save an answer for next time."
            />
          ) : (
            <ul
              className="max-h-[19rem] space-y-2 overflow-y-auto overscroll-contain pr-1"
              tabIndex={0}
              aria-label={`Knowledge gaps, ${detail.knowledgeGaps.length} total`}
            >
              {detail.knowledgeGaps.map((g) => (
                <li key={g.id} className="rounded-lg border border-hairline p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <KbStatusBadge status={g.status} />
                    {g.shortCode ? (
                      <code className="text-xs text-muted">{g.shortCode}</code>
                    ) : null}
                    <span className="ml-auto text-xs text-muted">
                      {fmtDateTime(g.createdAt)}
                    </span>
                  </div>
                  <p className="mt-1.5 text-sm font-medium text-ink">{g.question}</p>
                  {g.answer ? <p className="mt-1 text-sm text-ink-2">{g.answer}</p> : null}
                  {g.useCount > 0 ? (
                    <p className="mt-1 text-xs text-muted">Served {g.useCount}×</p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {openBooking ? (
        <BookingModal booking={openBooking} onClose={() => setOpenBooking(null)} />
      ) : null}
    </div>
  );
}
