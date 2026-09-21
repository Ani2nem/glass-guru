import { useState } from "react";
import { ApiError, api } from "../api";
import type { Triage } from "../types";

/**
 * Something went wrong; say what happened.
 *
 * Nothing the agent extracts is recorded until a dispatcher agrees to it. An event is
 * a fact in an append-only log, and a wrong one propagates into every plan that
 * follows - so the review step is a design decision rather than a courtesy.
 */
export function DisruptionPanel({ onChanged }: { onChanged: () => void }) {
  const [text, setText] = useState("");
  const [result, setResult] = useState<Triage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.triage(text));
    } catch (exc) {
      setError(exc as ApiError);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  async function accept() {
    if (!result) return;
    setBusy(true);
    try {
      await api.acceptTriage(result.events);
      setResult(null);
      setText("");
      onChanged();
    } catch (exc) {
      setError(exc as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel panel--disruption">
      <h2>Disruption</h2>
      <p className="panel__hint">Something broke or someone is out. Changes today's plan.</p>
      <textarea
        value={text}
        rows={3}
        placeholder="Dan called, van 3 won't start…"
        onChange={(e) => setText(e.target.value)}
      />
      <button className="primary" disabled={busy || !text.trim()} onClick={run}>
        {busy ? "Reading…" : "Read it"}
      </button>

      {error && (
        <p className="error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </p>
      )}

      {result && (
        <>
          {result.summary && <p>{result.summary}</p>}
          {result.events.length > 0 && (
            <ul className="events">
              {result.events.map((event, index) => (
                <li key={index}>
                  <code>{String(event.type)}</code>{" "}
                  {String(event.van_id ?? event.worker_id ?? event.job_id ?? "")}
                  {event.reason ? <span className="muted"> - {String(event.reason)}</span> : null}
                </li>
              ))}
            </ul>
          )}
          {result.question && <p className="warn">{result.question}</p>}
          {result.rejected.map((line) => (
            <p key={line} className="warn">{line}</p>
          ))}
          {result.repairs > 0 && (
            <p className="muted">The model needed {result.repairs} repair attempt(s).</p>
          )}
          {result.events.length > 0 && (
            <button className="primary" disabled={busy} onClick={accept}>
              Record {result.events.length} event(s)
            </button>
          )}
        </>
      )}
    </section>
  );
}
