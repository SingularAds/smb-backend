import { useState } from "react";
import {
  ApiError,
  fetchPairingStatus,
  generatePairing,
  type PairingResult,
  type PairingStatus,
} from "../api";
import { Badge } from "../components/Badge";

// Manual WhatsApp pairing — human-support tool. A support agent types the
// owner's phone number, generates a pairing code (or QR), and relays it to the
// owner through whatever channel is working. When the owner has an active
// onboarding session the backend arms the same auto-finalize poll Sofia uses,
// so onboarding completes the moment they link — no further owner action needed.

type Mode = "code" | "qr";

function StatusPill({ status }: { status?: string | null }) {
  const s = (status || "").toLowerCase();
  const tone =
    s === "connected" ? "good" : s === "needs_pairing" ? "warning" : "neutral";
  return <Badge tone={tone as "good" | "warning" | "neutral"}>{status || "unknown"}</Badge>;
}

export function PairingPage({ onAuthFail }: { onAuthFail: () => void }) {
  const [phone, setPhone] = useState("");
  const [mode, setMode] = useState<Mode>("code");
  const [busy, setBusy] = useState<"generate" | "status" | null>(null);
  const [result, setResult] = useState<PairingResult | null>(null);
  const [status, setStatus] = useState<PairingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const cleanedPhone = phone.replace(/[^\d]/g, "");
  const canSubmit = cleanedPhone.length >= 8 && busy === null;

  function handleError(err: unknown) {
    if (err instanceof ApiError && err.status === 401) return onAuthFail();
    setError(
      err instanceof ApiError
        ? `${err.status}: ${err.message}`
        : "Could not reach the backend. Is the API running?",
    );
  }

  async function doGenerate() {
    if (!canSubmit) return;
    setBusy("generate");
    setError(null);
    setResult(null);
    setCopied(false);
    try {
      const res = await generatePairing(cleanedPhone, mode);
      setResult(res);
      if (!res.ok && res.error) setError(res.error);
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(null);
    }
  }

  async function doStatus() {
    if (cleanedPhone.length < 8 || busy !== null) return;
    setBusy("status");
    setError(null);
    try {
      const res = await fetchPairingStatus(cleanedPhone);
      setStatus(res);
      if (!res.ok && res.error) setError(res.error);
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(null);
    }
  }

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — the code is visible to copy manually */
    }
  }

  return (
    <div className="space-y-4">
      <section className="card rise p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink">Manual WhatsApp pairing</h1>
          <span className="text-xs text-muted">human support · use when an owner can't pair in-chat</span>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted">
          Enter the owner's WhatsApp number, generate a pairing code (or QR), and
          relay it to them. They enter it in{" "}
          <strong>WhatsApp → Settings → Linked Devices → Link with phone number</strong>.
          If they already have an onboarding chat, setup finishes automatically the
          moment they link — no need to have them reply "done".
        </p>
      </section>

      <section className="card rise p-4">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void doGenerate();
          }}
        >
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-ink-2">
              Owner phone (country code + number)
            </span>
            <input
              value={phone}
              inputMode="tel"
              autoFocus
              placeholder="e.g. 5511998887777"
              onChange={(e) => setPhone(e.target.value)}
              className="w-64 rounded-md border border-hairline bg-page px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </label>

          <div role="group" aria-label="Pairing mode" className="flex gap-1.5">
            {(["code", "qr"] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={mode === m}
                onClick={() => setMode(m)}
                className={`rounded-full border px-3 py-1.5 text-xs font-medium ${
                  mode === m
                    ? "border-accent bg-accent text-white"
                    : "border-hairline text-ink-2 hover:bg-surface"
                }`}
              >
                {m === "code" ? "Pairing code" : "QR code"}
              </button>
            ))}
          </div>

          <button
            type="submit"
            disabled={!canSubmit}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy === "generate" ? "Generating…" : "Generate"}
          </button>
          <button
            type="button"
            onClick={doStatus}
            disabled={cleanedPhone.length < 8 || busy !== null}
            className="rounded-md border border-hairline px-4 py-2 text-sm font-medium text-ink-2 hover:bg-page disabled:opacity-40"
          >
            {busy === "status" ? "Checking…" : "Check status"}
          </button>
        </form>

        {error ? (
          <p role="alert" className="mt-3 text-xs font-medium text-[var(--status-critical)]">
            {error}
          </p>
        ) : null}

        {status && status.ok ? (
          <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 rounded-md border border-hairline bg-page px-3 py-2 text-xs text-ink-2">
            <span className="inline-flex items-center gap-1.5">
              Bridge status: <StatusPill status={status.status} />
            </span>
            {status.businessName ? <span>Business: <strong className="text-ink">{status.businessName}</strong></span> : null}
            {status.onboardingStep ? <span>Onboarding step: {status.onboardingStep}</span> : null}
            {status.pairedPhone ? <span>Linked to: +{status.pairedPhone}</span> : null}
            <span className="text-muted">session: {status.sessionId}</span>
          </div>
        ) : null}
      </section>

      {result && result.ok && result.alreadyConnected ? (
        <section className="card rise border-l-4 border-l-[var(--status-good)] p-4">
          <h2 className="text-sm font-semibold text-ink">Already connected ✅</h2>
          <p className="mt-1 text-xs text-ink-2">
            {result.message || "This number is already linked and connected — no pairing needed."}
          </p>
        </section>
      ) : null}

      {result && result.ok && result.mode === "code" && result.code ? (
        <section className="card rise p-5">
          <h2 className="text-sm font-semibold text-ink">Pairing code for +{result.phone}</h2>
          <div className="mt-3 flex items-center gap-3">
            <code className="select-all rounded-lg border border-hairline bg-page px-5 py-3 font-mono text-3xl font-bold tracking-widest text-ink">
              {result.code}
            </code>
            <button
              type="button"
              onClick={() => copyCode(result.code!)}
              className="rounded-md border border-hairline px-3 py-2 text-xs font-medium text-ink-2 hover:bg-page"
            >
              {copied ? "Copied ✓" : "Copy"}
            </button>
          </div>
          <PairingInstructions autoFinalize={result.autoFinalize} phone={result.phone} />
        </section>
      ) : null}

      {result && result.ok && result.mode === "qr" && result.qrDataUrl ? (
        <section className="card rise p-5">
          <h2 className="text-sm font-semibold text-ink">QR code for +{result.phone}</h2>
          <div className="mt-3 inline-block rounded-lg border border-hairline bg-white p-3">
            {/* eslint-disable-next-line jsx-a11y/img-redundant-alt */}
            <img src={result.qrDataUrl} alt="WhatsApp pairing QR code" width={220} height={220} />
          </div>
          <PairingInstructions autoFinalize={result.autoFinalize} phone={result.phone} isQr />
        </section>
      ) : null}
    </div>
  );
}

function PairingInstructions({
  autoFinalize,
  phone,
  isQr,
}: {
  autoFinalize?: boolean;
  phone?: string;
  isQr?: boolean;
}) {
  return (
    <div className="mt-4 space-y-2 text-xs text-ink-2">
      <p className="font-medium text-ink">Tell the owner to:</p>
      <ol className="ml-4 list-decimal space-y-1">
        <li>Open WhatsApp → <strong>Settings</strong> → <strong>Linked Devices</strong> → <strong>Link a Device</strong>.</li>
        {isQr ? (
          <li>Point their phone camera at this QR code to scan it.</li>
        ) : (
          <>
            <li>Tap <strong>"Link with phone number instead"</strong>.</li>
            <li>Enter their number (+{phone}), then type the code above.</li>
          </>
        )}
        <li className="text-muted">The code/QR expires in ~60 seconds — generate a fresh one if it lapses.</li>
      </ol>
      {autoFinalize ? (
        <p className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-accent-soft px-2 py-1 text-accent">
          ✓ Auto-finish is armed — onboarding completes automatically the moment they link.
        </p>
      ) : (
        <p className="mt-2 text-muted">
          No active onboarding session found for this number, so pairing won't auto-finish setup.
          Once they link, have them message Sofia so onboarding can complete.
        </p>
      )}
    </div>
  );
}
