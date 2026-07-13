import { useState } from "react";
import { ApiError, clearAdminKey, fetchGlobalKb, setAdminKey } from "../api";
import { XCircleIcon } from "./Icons";

// First-run gate: asks for the shared ANALYTICS_ADMIN_KEY. The key is
// verified against the API (a cheap GET) before we ever leave this screen,
// so a wrong key gets an explicit message instead of silently bouncing back
// here once a background fetch 401s. A later 401 still clears the key and
// returns here via the same onAuthFail path in App.tsx.

type Status = "idle" | "checking" | "error" | "success";

export function AdminKeyGate({ onSubmitted }: { onSubmitted: () => void }) {
  const [key, setKey] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);

  const checking = status === "checking";
  const busy = status === "checking" || status === "success";

  async function submit() {
    const trimmed = key.trim();
    if (!trimmed || busy) return;
    setStatus("checking");
    setError(null);
    setAdminKey(trimmed);
    try {
      await fetchGlobalKb();
      setStatus("success");
      // Autoredirect after 2 seconds if user doesn't click "Proceed" manually
      setTimeout(onSubmitted, 2000);
    } catch (err) {
      clearAdminKey();
      setStatus("error");
      setError(
        err instanceof ApiError && err.status === 401
          ? "Incorrect admin key. Double-check and try again."
          : "Could not reach the backend. Is the API running?",
      );
    }
  }

  if (status === "success") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
        <div 
          className="w-full max-w-sm rounded-xl bg-surface p-6 text-center border border-hairline rise"
          style={{ boxShadow: "var(--shadow-modal)" }}
        >
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-[var(--status-good)] text-white mb-4 shadow-md shadow-emerald-500/20">
            <svg
              className="h-7 w-7"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="3.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M20 6L9 17l-5-5" />
            </svg>
          </div>
          <h2 className="text-lg font-bold text-ink">Logged in successfully!</h2>
          <p className="mt-2 text-xs text-ink-2">
            Key verified. Welcome to the Recepte Internal Analytics dashboard.
          </p>
          <button
            onClick={onSubmitted}
            className="mt-6 w-full rounded bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-opacity-90 active:scale-[0.98] transition-all"
          >
            Proceed to Dashboard
          </button>
        </div>
      </div>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form
        className="w-full max-w-sm rounded-lg border border-hairline bg-surface p-6"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <h1 className="text-lg font-semibold text-ink">Internal analytics</h1>
        <p className="mt-1 text-sm text-ink-2">
          Enter the admin key (<code className="text-xs">ANALYTICS_ADMIN_KEY</code>)
          to continue. This dashboard is for the Recepte team only.
        </p>
        <label htmlFor="admin-key" className="mt-4 block text-xs font-medium text-ink-2">
          Admin key
        </label>
        <input
          id="admin-key"
          type="password"
          autoComplete="off"
          autoFocus
          value={key}
          onChange={(e) => {
            setKey(e.target.value);
            if (status !== "idle") {
              setStatus("idle");
              setError(null);
            }
          }}
          aria-invalid={status === "error"}
          aria-describedby={status === "error" ? "admin-key-error" : undefined}
          className={`mt-1 w-full rounded border bg-page px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 ${
            status === "error"
              ? "border-[var(--status-critical)] focus:ring-[var(--status-critical)]"
              : "border-hairline focus:ring-accent"
          }`}
        />
        {status === "error" && error ? (
          <p
            id="admin-key-error"
            role="alert"
            className="mt-2 flex items-center gap-1 text-xs font-medium text-[var(--status-critical)]"
          >
            <XCircleIcon /> {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={!key.trim() || busy}
          className="mt-4 w-full rounded bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {checking ? "Checking…" : "Open dashboard"}
        </button>
      </form>
    </main>
  );
}
