import { useState } from "react";
import { ApiError, api } from "../api";
import type { Candidate, Message, Repair } from "../types";

/**
 * The proposal queue.
 *
 * Several priced options rather than one answer, because choosing between "keep every
 * promise and serve less" and "serve more and make two calls" is a judgement about
 * this business today. The engine prices; a person picks.
 *
 * The autonomy verdict beside each candidate comes from deterministic rules, not from
 * the agent. Anything customer-visible needs a human however cheap it is, and the
 * override is explicit rather than a checkbox that quietly defaults on.
 */
function CandidateCard({
  candidate,
  onApply,
  onDraft,
  busy,
}: {
  candidate: Candidate;
  onApply: (strategy: string, force: boolean) => void;
  onDraft: (strategy: string) => void;
  busy: boolean;
}) {
  const auto = candidate.autonomy === "auto_apply";
  return (
    <article className={candidate.recommended ? "candidate candidate--rec" : "candidate"}>
      <header>
        <h3>{candidate.strategy.replace(/_/g, " ")}</h3>
        {candidate.recommended && <span className="tag">recommended</span>}
      </header>
      <p className="muted">{candidate.description}</p>

      <div className="metrics">
        <span><strong>{candidate.jobs_served}</strong> served</span>
        <span><strong>{candidate.changes}</strong> changes</span>
        <span className={candidate.customer_calls > 0 ? "warn" : ""}>
          <strong>{candidate.customer_calls}</strong> calls
        </span>
        <span className={`blast blast--${candidate.blast_radius}`}>
          {candidate.blast_radius.replace(/_/g, " ")}
        </span>
      </div>

      <ul className="diff">
        {candidate.diff.slice(0, 6).map((change) => (
          <li key={change.job_id} className={change.needs_customer_call ? "warn" : ""}>
            {change.needs_customer_call && <span className="tag tag--call">call</span>}
            {change.description}
          </li>
        ))}
        {candidate.diff.length > 6 && (
          <li className="muted">… {candidate.diff.length - 6} more</li>
        )}
      </ul>

      <p className={auto ? "muted" : "warn"}>
        {auto ? "May be applied automatically" : "Needs a dispatcher"}
        {candidate.autonomy_reasons.length > 0 && `: ${candidate.autonomy_reasons.join("; ")}`}
      </p>

      <div className="actions">
        <button disabled={busy} onClick={() => onApply(candidate.strategy, !auto)}>
          {auto ? "Apply" : "Approve and apply"}
        </button>
        {candidate.customer_calls > 0 && (
          <button disabled={busy} onClick={() => onDraft(candidate.strategy)}>
            Draft messages
          </button>
        )}
      </div>
    </article>
  );
}

export function ProposalPanel({ onChanged }: { onChanged: () => void }) {
  const [repair, setRepair] = useState<Repair | null>(null);
  const [messages, setMessages] = useState<Message[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function guarded(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (exc) {
      setError(exc as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel panel--fix">
      <h2>Fix the day</h2>
      <p className="panel__hint">
        Prices the ways out of a disruption. Two or three options, never one answer.
      </p>
      <button
        className="primary"
        disabled={busy}
        onClick={() => guarded(async () => setRepair(await api.repair()))}
      >
        {busy ? "Working…" : "Repair the plan"}
      </button>

      {error && (
        <p className="error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </p>
      )}

      {repair?.candidates.map((candidate) => (
        <CandidateCard
          key={candidate.strategy}
          candidate={candidate}
          busy={busy}
          onApply={(strategy, force) =>
            guarded(async () => {
              await api.applyRepair(strategy, force);
              setRepair(null);
              setMessages(null);
              onChanged();
            })
          }
          onDraft={(strategy) =>
            guarded(async () => setMessages(await api.comms(strategy)))
          }
        />
      ))}

      {messages && (
        <div className="messages">
          <h3>Customer messages</h3>
          {messages.map((message) => (
            <article key={message.job_id} className={message.grounded ? "msg" : "msg msg--held"}>
              <header>
                <code>{message.channel}</code> {message.job_id}
                {!message.grounded && <span className="tag tag--call">held</span>}
              </header>
              <p>{message.body}</p>
              {/* A draft is held when it states a time the plan does not support.
                  A plausible wrong time in a text is worse than no text. */}
              {message.issues.map((issue) => (
                <p key={issue} className="error">{issue}</p>
              ))}
            </article>
          ))}
          {messages.length === 0 && <p className="muted">Nobody needs telling.</p>}
        </div>
      )}
    </section>
  );
}
