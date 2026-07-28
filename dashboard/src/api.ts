import type {
  BusinessDetail,
  GlobalKb,
  OnboardingChat,
  Overview,
  RangeFilter,
} from "./types";

// The admin key lives in sessionStorage only (cleared when the tab closes) —
// this is an internal tool behind a shared key, not user auth.
const KEY_STORAGE = "recepte-admin-key";

export function getAdminKey(): string | null {
  return sessionStorage.getItem(KEY_STORAGE);
}

export function setAdminKey(key: string): void {
  sessionStorage.setItem(KEY_STORAGE, key);
}

export function clearAdminKey(): void {
  sessionStorage.removeItem(KEY_STORAGE);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function rangeParams(filter: RangeFilter): URLSearchParams {
  const params = new URLSearchParams();
  if (filter.from) {
    params.set("from", filter.from);
    if (filter.to) params.set("to", filter.to);
  } else {
    params.set("days", String(filter.preset ?? 30));
  }
  return params;
}

async function request<T>(
  method: "GET" | "PUT" | "POST",
  path: string,
  params?: URLSearchParams,
  body?: unknown,
): Promise<T> {
  const key = getAdminKey();
  const qs = params?.toString() ?? "";
  const headers: Record<string, string> = key ? { "x-admin-key": key } : {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1/analytics${path}${qs ? `?${qs}` : ""}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const errBody = await res.json();
      if (typeof errBody?.detail === "string") detail = errBody.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const apiGet = <T,>(path: string, params: URLSearchParams) =>
  request<T>("GET", path, params);

export function fetchOverview(
  filter: RangeFilter,
  includeTest: boolean,
  globalDevice?: string | null,
  funnelRange?: RangeFilter | null,
): Promise<Overview> {
  const params = rangeParams(filter);
  if (includeTest) params.set("include_test", "true");
  // Omitted entirely when null → backend returns all numbers combined.
  if (globalDevice) params.set("global_device", globalDevice);
  // The funnel carries its own window. Sending NO funnel_* param is what
  // asks the backend for all time, so an all-time selection stays silent.
  if (funnelRange) {
    if (funnelRange.from) {
      params.set("funnel_from", funnelRange.from);
      if (funnelRange.to) params.set("funnel_to", funnelRange.to);
    } else if (funnelRange.preset) {
      params.set("funnel_days", String(funnelRange.preset));
    }
  }
  return apiGet<Overview>("/overview", params);
}

export function fetchBusinessDetail(
  businessId: string,
  filter: RangeFilter,
): Promise<BusinessDetail> {
  return apiGet<BusinessDetail>(
    `/businesses/${encodeURIComponent(businessId)}`,
    rangeParams(filter),
  );
}

/** Download the onboarding prospects in [from, to] as an .xlsx file.
 *  Both dates are required (the backend rejects a missing range with 422).
 *  Mirrors the dashboard funnel (demo/test excluded, same number scope);
 *  triggers a browser download named by the server's Content-Disposition. */
export async function downloadOnboardingExport(
  from: string,
  to: string,
  globalDevice?: string | null,
): Promise<void> {
  const key = getAdminKey();
  const params = new URLSearchParams({ from, to });
  if (globalDevice) params.set("global_device", globalDevice);
  const res = await fetch(`/api/v1/analytics/onboarding-export?${params}`, {
    headers: key ? { "x-admin-key": key } : {},
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const errBody = await res.json();
      if (typeof errBody?.detail === "string") detail = errBody.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const filename =
    /filename="([^"]+)"/.exec(disposition)?.[1] ??
    `onboarding_${from}_to_${to ?? "today"}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** The owner↔Sofia onboarding conversation for one prospect phone, read from
 *  the onboarding_transcripts archive (templates included). Powers the funnel
 *  drill-down's "view conversation" button. Rejects with a 404 ApiError when
 *  no messages exist for that phone. */
export function fetchOnboardingChat(phone: string): Promise<OnboardingChat> {
  const params = new URLSearchParams({ phone });
  return apiGet<OnboardingChat>("/onboarding-chat", params);
}

export function fetchGlobalKb(): Promise<GlobalKb> {
  return request<GlobalKb>("GET", "/global-kb");
}

export function saveGlobalKb(content: string): Promise<GlobalKb> {
  return request<GlobalKb>("PUT", "/global-kb", undefined, { content });
}

// ── Manual WhatsApp pairing (human-support tool) ────────────────────────────

export interface PairingResult {
  ok: boolean;
  error?: string;
  message?: string;
  mode?: "code" | "qr";
  code?: string;
  qrDataUrl?: string;
  sessionId?: string;
  phone?: string;
  autoFinalize?: boolean;
  alreadyConnected?: boolean;
  status?: string;
  /** Transient first-attempt socket warm-up — show as a soft "try again", not an error. */
  retryable?: boolean;
  warning?: string;
}

export interface PairingStatus {
  ok: boolean;
  error?: string;
  sessionId?: string;
  phone?: string;
  status?: string | null;
  paired?: boolean;
  pairedPhone?: string;
  businessName?: string;
  hasBusiness?: boolean;
  onboardingStep?: string;
}

export function generatePairing(
  phone: string,
  mode: "code" | "qr",
): Promise<PairingResult> {
  return request<PairingResult>("POST", "/pairing/generate", undefined, {
    phone,
    mode,
  });
}

export function fetchPairingStatus(phone: string): Promise<PairingStatus> {
  const params = new URLSearchParams({ phone });
  return request<PairingStatus>("GET", "/pairing/status", params);
}
