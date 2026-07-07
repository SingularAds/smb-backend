// Shared display formatters for the analytics dashboard. Every function is
// null-safe — API fields documented as nullable in ../types.ts really are
// null in production Firestore data, so each formatter renders "—" instead
// of throwing or printing "null"/"NaN".

const dateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});

const dateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const numberFormatter = new Intl.NumberFormat("en-US");

const relativeFormatter = new Intl.RelativeTimeFormat("en-US", { numeric: "auto" });

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 60 * 60],
  ["month", 30 * 24 * 60 * 60],
  ["week", 7 * 24 * 60 * 60],
  ["day", 24 * 60 * 60],
  ["hour", 60 * 60],
  ["minute", 60],
];

/**
 * Parse an API date string into a Date. Plain "YYYY-MM-DD" values (as used
 * by GrowthPoint/ActivityPoint) are parsed as local midnight rather than UTC
 * midnight — the native Date constructor treats date-only strings as UTC,
 * which shifts the displayed calendar day backward in negative-UTC
 * timezones. Full ISO timestamps are parsed as-is.
 */
function toDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (dateOnly) {
    const [, y, m, d] = dateOnly;
    return new Date(Number(y), Number(m) - 1, Number(d));
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function fmtDate(value: string | null | undefined): string {
  const d = toDate(value);
  return d ? dateFormatter.format(d) : "—";
}

export function fmtDateTime(value: string | null | undefined): string {
  const d = toDate(value);
  return d ? dateTimeFormatter.format(d) : "—";
}

export function fmtRelative(value: string | null | undefined): string {
  const d = toDate(value);
  if (!d) return "—";
  const diffSeconds = (d.getTime() - Date.now()) / 1000;
  const absSeconds = Math.abs(diffSeconds);
  if (absSeconds < 60) return "just now";
  for (const [unit, secondsInUnit] of RELATIVE_UNITS) {
    if (absSeconds >= secondsInUnit) {
      return relativeFormatter.format(Math.round(diffSeconds / secondsInUnit), unit);
    }
  }
  return "just now";
}

export function fmtNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return numberFormatter.format(value);
}

/** `value` is a 0–1 fraction (e.g. 0.42), formatted as "42%". */
export function fmtPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const totalSeconds = Math.max(0, Math.round(value));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** WhatsApp/owner numbers are stored as bare digits (no "+"); normalize display to "+<digits>". */
export function fmtPhone(value: string | null | undefined): string {
  if (!value) return "—";
  const trimmed = value.trim();
  if (!trimmed) return "—";
  const digits = trimmed.replace(/\D/g, "");
  return digits ? `+${digits}` : trimmed;
}

/** snake_case / kebab-case token → Title Case, e.g. "no_show" -> "No Show". */
export function fmtToken(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .replace(/[_-]+/g, " ")
    .trim()
    .split(" ")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}
