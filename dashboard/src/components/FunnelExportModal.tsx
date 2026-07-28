import { useState } from "react";
import { Modal } from "./Modal";
import { DownloadIcon } from "./Icons";
import { downloadOnboardingExport, ApiError } from "../api";

// Export dialog for the onboarding funnel: pick a date range, download the
// prospects in it as an .xlsx workbook. The export mirrors the funnel exactly
// (demo/test sessions excluded, current global-number scope applied) — one
// row per onboarding journey with owner contact + business + stage details.
//
// Both dates start EMPTY and must be chosen explicitly — the download stays
// disabled until a valid start+end range is picked. This prevents a silent
// "why did I get last-30-days data I never asked for?" download from a
// pre-filled default.

// Today (local) as YYYY-MM-DD — caps both inputs so no future date is picked.
function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

export function FunnelExportModal({
  globalDevice,
  onClose,
}: {
  /** current global-number scope (null = all numbers) — applied to the export */
  globalDevice?: string | null;
  onClose: () => void;
}) {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const today = todayISO();
  const bothPicked = Boolean(from) && Boolean(to);
  const orderValid = !bothPicked || from <= to;
  const canDownload = bothPicked && orderValid && !busy;

  // A single, plain-language reason the download is blocked (null when ready).
  const hint = !bothPicked
    ? "Please select both a start and an end date to download."
    : !orderValid
      ? "The start date must be on or before the end date."
      : null;

  async function handleDownload() {
    if (!canDownload) return;
    setBusy(true);
    setError(null);
    try {
      await downloadOnboardingExport(from, to, globalDevice);
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Download failed — try again.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Export onboarding data"
      subtitle="Downloads an Excel file with one row per onboarding journey — owner contact, business details, current stage, pairing state and acquisition source."
      onClose={onClose}
      width="max-w-md"
    >
      <div className="space-y-4 px-5 py-4">
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs text-muted">
            From <span className="text-[var(--negative,#b3261e)]">*</span>
            <input
              type="date"
              value={from}
              max={to || today}
              onChange={(e) => setFrom(e.target.value)}
              aria-required="true"
              aria-invalid={!from}
              className="mt-1 w-full rounded-md border border-hairline bg-page px-2.5 py-1.5 text-sm text-ink"
            />
          </label>
          <label className="block text-xs text-muted">
            To <span className="text-[var(--negative,#b3261e)]">*</span>
            <input
              type="date"
              value={to}
              min={from || undefined}
              max={today}
              onChange={(e) => setTo(e.target.value)}
              aria-required="true"
              aria-invalid={!to}
              className="mt-1 w-full rounded-md border border-hairline bg-page px-2.5 py-1.5 text-sm text-ink"
            />
          </label>
        </div>

        <p className="text-[11px] leading-relaxed text-muted">
          {globalDevice
            ? "Scoped to the currently selected global number. Clear the number filter first to export all numbers."
            : "Covers all global onboarding numbers."}{" "}
          Demo and test sessions are excluded, matching the funnel on screen.
        </p>

        {/* Blocking reason (missing/invalid dates) or a server error. */}
        {error ? (
          <p role="alert" className="text-xs text-[var(--negative,#b3261e)]">
            {error}
          </p>
        ) : hint ? (
          <p role="status" className="text-xs text-muted">
            {hint}
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pb-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-hairline px-3 py-1.5 text-xs font-medium text-ink-2 hover:bg-page"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canDownload}
            onClick={handleDownload}
            title={hint ?? undefined}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <DownloadIcon />
            {busy ? "Preparing…" : "Download .xlsx"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
