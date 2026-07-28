import type { BusinessDetail, GlobalKb, Overview, RangeFilter } from "./types";

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
