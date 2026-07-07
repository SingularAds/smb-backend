import type { ReactNode } from "react";
import {
  AlertTriangleIcon,
  CheckCircleIcon,
  ClockIcon,
  XCircleIcon,
} from "./Icons";

// Status colors are reserved for state and NEVER carry meaning alone —
// every badge pairs the color with an icon and a text label.

type Tone = "good" | "warning" | "serious" | "critical" | "neutral";

const toneStyle: Record<Tone, { color: string; bg: string }> = {
  good: { color: "var(--status-good)", bg: "rgba(12,163,12,0.10)" },
  warning: { color: "var(--status-warning)", bg: "rgba(250,178,25,0.14)" },
  serious: { color: "var(--status-serious)", bg: "rgba(236,131,90,0.14)" },
  critical: { color: "var(--status-critical)", bg: "rgba(208,59,59,0.12)" },
  neutral: { color: "var(--text-secondary)", bg: "var(--gridline)" },
};

const toneIcon: Record<Tone, ReactNode> = {
  good: <CheckCircleIcon />,
  warning: <ClockIcon />,
  serious: <AlertTriangleIcon />,
  critical: <XCircleIcon />,
  neutral: null,
};

export function Badge({
  tone,
  children,
  icon,
}: {
  tone: Tone;
  children: ReactNode;
  icon?: ReactNode;
}) {
  const s = toneStyle[tone];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap"
      style={{ backgroundColor: s.bg }}
    >
      <span style={{ color: s.color }} className="inline-flex">
        {icon !== undefined ? icon : toneIcon[tone]}
      </span>
      {/* Text wears text tokens, never the status color */}
      <span className="text-ink-2">{children}</span>
    </span>
  );
}

export function PairedBadge({ paired }: { paired: boolean }) {
  return paired ? (
    <Badge tone="good">Paired</Badge>
  ) : (
    <Badge tone="neutral" icon={<XCircleIcon />}>
      Not paired
    </Badge>
  );
}

export function PlanBadge({
  plan,
  trialDaysLeft,
}: {
  plan: string | null;
  trialDaysLeft: number | null;
}) {
  if (!plan) return <Badge tone="neutral">No plan</Badge>;
  const isTrial = plan === "trial" || plan === "trialing";
  if (isTrial) {
    if (trialDaysLeft !== null && trialDaysLeft < 0) {
      return <Badge tone="critical">Trial expired</Badge>;
    }
    if (trialDaysLeft !== null && trialDaysLeft <= 3) {
      return <Badge tone="warning">Trial · {trialDaysLeft}d left</Badge>;
    }
    return (
      <Badge tone="neutral" icon={<ClockIcon />}>
        Trial{trialDaysLeft !== null ? ` · ${trialDaysLeft}d left` : ""}
      </Badge>
    );
  }
  return <Badge tone="good">{plan}</Badge>;
}

export function BookingStatusBadge({ status }: { status: string }) {
  switch (status) {
    case "confirmed":
    case "completed":
      return <Badge tone="good">{status}</Badge>;
    case "pending":
      return <Badge tone="warning">pending</Badge>;
    case "no_show":
      return <Badge tone="serious">no-show</Badge>;
    case "cancelled":
      return <Badge tone="critical">cancelled</Badge>;
    default:
      return <Badge tone="neutral">{status}</Badge>;
  }
}

export function ComplaintStatusBadge({ status }: { status: string }) {
  return status === "open" ? (
    <Badge tone="critical">open</Badge>
  ) : (
    <Badge tone="good">resolved</Badge>
  );
}

export function KbStatusBadge({ status }: { status: string }) {
  switch (status) {
    case "confirmed":
      return <Badge tone="good">confirmed</Badge>;
    case "pending_review":
      return <Badge tone="warning">pending review</Badge>;
    case "awaiting_answer":
      return <Badge tone="warning">awaiting answer</Badge>;
    case "rejected":
      return <Badge tone="neutral" icon={<XCircleIcon />}>rejected</Badge>;
    case "expired":
      return <Badge tone="neutral" icon={<ClockIcon />}>expired</Badge>;
    default:
      return <Badge tone="neutral">{status}</Badge>;
  }
}
