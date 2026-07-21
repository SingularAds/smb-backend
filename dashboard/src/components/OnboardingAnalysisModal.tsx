import { useCallback, useEffect, useState } from "react";
import { Modal } from "./Modal";
import { Badge } from "./Badge";
import { analyzeOnboardingSession, sendAnalysisFeedback, ApiError } from "../api";
import type {
  AnalysisOutcome,
  DropOffReason,
  OnboardingAnalysisResult,
} from "../types";
import { fmtPhone, fmtRelative } from "../lib/format";

// AI analysis of one owner's onboarding journey — opened from the funnel
// drill-down's "Analyze" button. Fresh analyses call the LLM (5–20s); repeat
// opens are served from the backend's Firestore cache instantly. The team can
// re-run (force) and leave 👍/👎 feedback that feeds prompt iteration.

const OUTCOME_TONE: Record<AnalysisOutcome, "good" | "critical" | "warning"> = {
  completed: "good",
  dropped: "critical",
  still_active: "warning",
};

const OUTCOME_LABEL: Record<AnalysisOutcome, string> = {
  completed: "Completed onboarding",
  dropped: "Dropped off",
  still_active: "Still active",
};

const REASON_LABEL: Record<DropOffReason, string> = {
  PRICING_CONCERN: "Pricing concern",
  FEATURE_MISMATCH: "Feature mismatch",
  CONFUSION_FRICTION: "Confusion / flow friction",
  TRUST_HESITATION: "Trust hesitation",
  LOST_INTEREST_INACTIVE: "Lost interest / went inactive",
  LANGUAGE_BARRIER: "Language barrier",
  TECHNICAL_ISSUE: "Technical issue",
  PAIRING_ABANDONED: "Abandoned at WhatsApp pairing",
  PAYMENT_ABANDONED: "Abandoned at payment",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
  OTHER: "Other",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="px-5 py-3.5 border-b border-hairline last:border-0">
      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
        {title}
      </h3>
      {children}
    </section>
  );
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="space-y-1 text-sm text-ink-2">
      {items.map((item, i) => (
        <li key={i} className="flex gap-2">
          <span aria-hidden="true" className="text-muted">•</span>
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

export function OnboardingAnalysisModal({
  phone,
  name,
  onClose,
}: {
  phone: string;
  name: string | null;
  onClose: () => void;
}) {
  const [result, setResult] = useState<OnboardingAnalysisResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [feedbackSent, setFeedbackSent] = useState<"up" | "down" | null>(null);

  const run = useCallback(
    (force: boolean) => {
      setLoading(true);
      setError(null);
      if (force) setFeedbackSent(null);
      analyzeOnboardingSession(phone, force)
        .then(setResult)
        .catch((err) => {
          setError(
            err instanceof ApiError ? err.message : "Analysis request failed",
          );
        })
        .finally(() => setLoading(false));
    },
    [phone],
  );

  useEffect(() => run(false), [run]);

  function handleFeedback(helpful: boolean) {
    setFeedbackSent(helpful ? "up" : "down");
    // Fire-and-forget — feedback failing should never disrupt reading.
    sendAnalysisFeedback(phone, helpful).catch(() => setFeedbackSent(null));
  }

  const a = result?.analysis;

  return (
    <Modal
      title={`Onboarding analysis — ${name ?? fmtPhone(phone)}`}
      subtitle={
        result
          ? `${result.messageCount} messages analyzed · ${result.model} · ` +
            `${result.cached ? "cached " : ""}${fmtRelative(result.analyzedAt)}`
          : fmtPhone(phone)
      }
      onClose={onClose}
      width="max-w-2xl"
      footer={
        result && !loading ? (
          <>
            <span className="mr-auto flex items-center gap-1 text-xs text-muted">
              Was this analysis helpful?
              <button
                type="button"
                onClick={() => handleFeedback(true)}
                disabled={feedbackSent !== null}
                aria-label="Analysis was helpful"
                className={`rounded px-1.5 py-0.5 hover:bg-[var(--page)] disabled:cursor-default ${
                  feedbackSent === "up" ? "bg-[var(--page)]" : ""
                }`}
              >
                👍
              </button>
              <button
                type="button"
                onClick={() => handleFeedback(false)}
                disabled={feedbackSent !== null}
                aria-label="Analysis was not helpful"
                className={`rounded px-1.5 py-0.5 hover:bg-[var(--page)] disabled:cursor-default ${
                  feedbackSent === "down" ? "bg-[var(--page)]" : ""
                }`}
              >
                👎
              </button>
              {feedbackSent ? <span>Thanks!</span> : null}
            </span>
            <button
              type="button"
              onClick={() => run(true)}
              className="rounded-md border border-[var(--hairline)] px-2.5 py-1 text-xs font-medium text-ink-2 hover:bg-[var(--page)]"
            >
              Re-analyze
            </button>
          </>
        ) : undefined
      }
    >
      {loading ? (
        <div className="px-5 py-12 text-center">
          <div
            aria-hidden="true"
            className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-[var(--hairline)]"
            style={{ borderTopColor: "var(--accent)" }}
          />
          <p className="text-sm text-ink-2">Analyzing the conversation…</p>
          <p className="mt-1 text-xs text-muted">
            Fresh analyses take 5–20 seconds; repeat opens are instant.
          </p>
        </div>
      ) : error ? (
        <div className="px-5 py-10 text-center">
          <p className="text-sm text-ink-2">{error}</p>
          <button
            type="button"
            onClick={() => run(false)}
            className="mt-3 rounded-md px-3 py-1 text-xs font-medium"
            style={{ background: "var(--accent)", color: "#fff" }}
          >
            Retry
          </button>
        </div>
      ) : a ? (
        <div>
          {/* Verdict header */}
          <div className="flex flex-wrap items-center gap-2 border-b border-hairline px-5 py-3.5">
            <Badge tone={OUTCOME_TONE[a.outcome]}>{OUTCOME_LABEL[a.outcome]}</Badge>
            {a.dropOffReason ? (
              <Badge tone="neutral">{REASON_LABEL[a.dropOffReason]}</Badge>
            ) : null}
            {a.dropOffStage ? (
              <span className="text-xs text-muted">at “{a.dropOffStage}”</span>
            ) : null}
            <span className="ml-auto text-xs text-muted">
              confidence: <span className="font-medium text-ink-2">{a.confidence}</span>
            </span>
          </div>

          <Section title="Summary">
            <p className="text-sm leading-relaxed text-ink">{a.summary}</p>
          </Section>

          <Section title="Customer intent">
            <p className="text-sm text-ink-2">{a.customerIntent}</p>
          </Section>

          {a.evidence.length > 0 ? (
            <Section title="Evidence from the conversation">
              <ul className="space-y-1.5">
                {a.evidence.map((quote, i) => (
                  <li
                    key={i}
                    className="border-l-2 pl-2.5 text-sm italic text-ink-2"
                    style={{ borderColor: "var(--accent)" }}
                  >
                    {quote}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {a.objections.length > 0 ? (
            <Section title="Objections voiced">
              <BulletList items={a.objections} />
            </Section>
          ) : null}

          {a.frictionPoints.length > 0 ? (
            <Section title="Friction points">
              <BulletList items={a.frictionPoints} />
            </Section>
          ) : null}

          {a.recommendations.length > 0 ? (
            <Section title="Recommended flow improvements">
              <ol className="space-y-1 text-sm text-ink-2">
                {a.recommendations.map((rec, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="font-semibold text-ink">{i + 1}.</span>
                    <span>{rec}</span>
                  </li>
                ))}
              </ol>
            </Section>
          ) : null}

          {result.transcriptSource === "session_history" ? (
            <div className="px-5 py-3 text-xs text-muted">
              Based on the session's stored history (this journey predates the
              transcript archive) — early messages may be missing.
            </div>
          ) : null}
        </div>
      ) : null}
    </Modal>
  );
}
