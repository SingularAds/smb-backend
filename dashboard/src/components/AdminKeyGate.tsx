import { useState } from "react";
import { setAdminKey } from "../api";

// First-run gate: asks for the shared ANALYTICS_ADMIN_KEY and stores it in
// sessionStorage. A 401 from the API clears the key and returns here.

export function AdminKeyGate({ onSubmitted }: { onSubmitted: () => void }) {
  const [key, setKey] = useState("");

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form
        className="w-full max-w-sm rounded-lg border border-hairline bg-surface p-6"
        onSubmit={(e) => {
          e.preventDefault();
          if (!key.trim()) return;
          setAdminKey(key.trim());
          onSubmitted();
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
          value={key}
          onChange={(e) => setKey(e.target.value)}
          className="mt-1 w-full rounded border border-hairline bg-page px-3 py-2 text-sm text-ink"
        />
        <button
          type="submit"
          disabled={!key.trim()}
          className="mt-4 w-full rounded bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          Open dashboard
        </button>
      </form>
    </main>
  );
}
