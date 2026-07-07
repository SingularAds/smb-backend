import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, fetchGlobalKb, saveGlobalKb } from "../api";
import type { GlobalKb } from "../types";
import { Badge } from "../components/Badge";
import { Modal } from "../components/Modal";
import { CheckCircleIcon } from "../components/Icons";
import { fmtDateTime, fmtNumber } from "../lib/format";

// Global Recepte KB editor. The KB is ONE text document injected into the
// sales/onboarding AI's prompt, structured with ALL-CAPS section headings.
// We parse it into sections for organized editing and reassemble losslessly
// on save; "Raw text" mode edits the whole document for structural changes
// (adding/removing/reordering sections).

interface KbSection {
  id: number;
  title: string | null; // null = preamble (the === wrapper line etc.)
  body: string;
}

const HEADING_RE = /^[A-Z0-9 ?!&'’“”"/,.:()+-]+$/;

function isHeading(line: string): boolean {
  const t = line.trim();
  if (!t || t.length > 64) return false;
  if (t.startsWith("===")) return false; // document wrapper lines stay in body
  if (!/[A-Z]/.test(t)) return false;
  return HEADING_RE.test(t) && t === t.toUpperCase();
}

function parseSections(content: string): KbSection[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const sections: KbSection[] = [];
  let current: KbSection = { id: 0, title: null, body: "" };
  let currentLines: string[] = [];
  let nextId = 1;

  const flush = () => {
    current.body = currentLines.join("\n");
    if (current.title !== null || current.body.trim() !== "") {
      sections.push(current);
    }
    currentLines = [];
  };

  for (const line of lines) {
    if (isHeading(line)) {
      flush();
      current = { id: nextId++, title: line, body: "" };
    } else {
      currentLines.push(line);
    }
  }
  flush();
  return sections;
}

function assembleSections(sections: KbSection[]): string {
  return sections
    .flatMap((s) => (s.title !== null ? [s.title, s.body] : [s.body]))
    .join("\n");
}

function textareaRows(text: string): number {
  return Math.min(Math.max(text.split("\n").length + 1, 3), 24);
}

function KbSkeleton() {
  return (
    <div role="status" aria-label="Loading knowledge base" className="space-y-3">
      <span className="sr-only">Loading knowledge base…</span>
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="card p-4">
          <div className="skeleton h-4 w-48" aria-hidden="true" />
          <div className="skeleton mt-3 h-24 w-full" aria-hidden="true" />
        </div>
      ))}
    </div>
  );
}

export function GlobalKbPage({ onAuthFail }: { onAuthFail: () => void }) {
  const [kb, setKb] = useState<GlobalKb | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"sections" | "raw">("sections");
  const [sections, setSections] = useState<KbSection[]>([]);
  const [rawText, setRawText] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const baselineRef = useRef("");

  useEffect(() => {
    let cancelled = false;
    fetchGlobalKb().then(
      (data) => {
        if (cancelled) return;
        setKb(data);
        baselineRef.current = data.content;
        setSections(parseSections(data.content));
        setRawText(data.content);
      },
      (err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) return onAuthFail();
        setError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the backend.");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [onAuthFail]);

  const currentContent = useMemo(
    () => (mode === "raw" ? rawText : assembleSections(sections)),
    [mode, rawText, sections],
  );
  const dirty = kb !== null && currentContent !== baselineRef.current;
  const overCap = kb !== null && currentContent.length > kb.maxChars;

  function switchMode(next: "sections" | "raw") {
    if (next === mode) return;
    // Carry edits across modes so nothing is lost
    if (next === "raw") setRawText(assembleSections(sections));
    else setSections(parseSections(rawText));
    setMode(next);
  }

  async function doSave() {
    if (!kb) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await saveGlobalKb(currentContent);
      setKb(updated);
      baselineRef.current = updated.content;
      setSections(parseSections(updated.content));
      setRawText(updated.content);
      setSavedAt(Date.now());
      setConfirming(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onAuthFail();
      setError(err instanceof ApiError ? err.message : "Save failed — is the backend running?");
      setConfirming(false);
    } finally {
      setSaving(false);
    }
  }

  if (error && !kb) {
    return (
      <div role="alert" className="card p-6 text-sm text-ink-2">
        <p className="font-medium text-ink">Could not load the knowledge base</p>
        <p className="mt-1">{error}</p>
      </div>
    );
  }
  if (!kb) return <KbSkeleton />;

  return (
    <div className="space-y-4">
      <section className="card rise p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink">Global knowledge base</h1>
          {kb.source === "default" ? (
            <Badge tone="warning">seed default — not yet saved to Firestore</Badge>
          ) : (
            <Badge tone="good">live</Badge>
          )}
          {dirty ? <Badge tone="warning">unsaved changes</Badge> : null}
        </div>
        <p className="mt-1 max-w-3xl text-xs text-muted">
          This text is injected into the onboarding/sales AI's prompt for{" "}
          <strong>every</strong> prospect conversation (product, pricing, FAQ,
          objection handling, brand voice). Saving takes effect immediately —
          other server workers pick it up within 5 minutes.
        </p>
        <dl className="mt-3 flex flex-wrap gap-x-8 gap-y-2 text-xs">
          <div>
            <dt className="text-muted">Last updated</dt>
            <dd className="text-ink-2">{kb.updatedAt ? fmtDateTime(kb.updatedAt) : "never (seed default)"}</dd>
          </div>
          <div>
            <dt className="text-muted">Size</dt>
            <dd className={overCap ? "font-medium text-[var(--status-critical)]" : "text-ink-2"}>
              {fmtNumber(currentContent.length)} / {fmtNumber(kb.maxChars)} chars
              {" · "}~{fmtNumber(Math.max(1, Math.floor(currentContent.length / 4)))} tokens per prompt
            </dd>
          </div>
        </dl>
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Editor mode" className="flex gap-1.5">
          {(["sections", "raw"] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => switchMode(m)}
              className={`rounded-full border px-3 py-1 text-xs font-medium ${
                mode === m
                  ? "border-accent bg-accent text-white"
                  : "border-hairline text-ink-2 hover:bg-surface"
              }`}
            >
              {m === "sections" ? "By section" : "Raw text"}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {savedAt && !dirty ? (
            <span className="inline-flex items-center gap-1 text-xs text-[var(--status-good)]">
              <CheckCircleIcon /> Saved
            </span>
          ) : null}
          {error ? <span className="text-xs text-[var(--status-critical)]">{error}</span> : null}
          <button
            type="button"
            disabled={!dirty || overCap || saving}
            onClick={() => setConfirming(true)}
            className="rounded-md bg-accent px-4 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>

      {mode === "sections" ? (
        <div className="space-y-3">
          {sections.map((s, idx) => (
            <section key={s.id} className="card rise p-4" style={{ animationDelay: `${Math.min(idx, 8) * 50}ms` }}>
              {s.title !== null ? (
                <label className="block">
                  <span className="sr-only">Section title</span>
                  <input
                    value={s.title}
                    onChange={(e) =>
                      setSections((prev) =>
                        prev.map((x) => (x.id === s.id ? { ...x, title: e.target.value } : x)),
                      )
                    }
                    className="w-full rounded-md border border-transparent bg-transparent text-sm font-semibold text-ink hover:border-hairline focus:border-hairline focus:bg-page"
                  />
                </label>
              ) : (
                <h2 className="text-xs font-medium text-muted">Document header</h2>
              )}
              <label className="mt-2 block">
                <span className="sr-only">
                  {s.title ? `Content of section ${s.title}` : "Document header content"}
                </span>
                <textarea
                  value={s.body}
                  rows={textareaRows(s.body)}
                  onChange={(e) =>
                    setSections((prev) =>
                      prev.map((x) => (x.id === s.id ? { ...x, body: e.target.value } : x)),
                    )
                  }
                  className="w-full resize-y rounded-md border border-hairline bg-page px-3 py-2 font-mono text-xs leading-relaxed text-ink-2"
                />
              </label>
            </section>
          ))}
          <p className="text-xs text-muted">
            Section headings are the ALL-CAPS lines. To add, remove, or reorder
            sections, switch to <strong>Raw text</strong>.
          </p>
        </div>
      ) : (
        <section className="card rise p-4">
          <label className="block">
            <span className="sr-only">Knowledge base raw text</span>
            <textarea
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              rows={32}
              className="w-full resize-y rounded-md border border-hairline bg-page px-3 py-2 font-mono text-xs leading-relaxed text-ink-2"
            />
          </label>
        </section>
      )}

      {confirming ? (
        <Modal
          title="Save global knowledge base?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded-md border border-hairline px-3 py-1.5 text-xs font-medium text-ink-2 hover:bg-page"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={doSave}
                disabled={saving}
                className="rounded-md bg-accent px-4 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "Saving…" : "Yes, update the live KB"}
              </button>
            </>
          }
        >
          <div className="px-5 py-4 text-sm text-ink-2">
            <p>
              This immediately changes what the AI tells prospects about the
              product, pricing, and onboarding — in live WhatsApp
              conversations.
            </p>
            <p className="mt-2 text-xs text-muted">
              New size: {fmtNumber(currentContent.length)} characters (~
              {fmtNumber(Math.max(1, Math.floor(currentContent.length / 4)))} tokens
              added to every sales prompt).
            </p>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
