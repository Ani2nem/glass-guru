import { useState } from "react";
import { ApiError, api } from "../api";
import type { Intake } from "../types";

/**
 * The live-call copilot.
 *
 * A dispatcher types while the customer talks. What comes back is deliberately in two
 * halves: what was captured, and what still has to be asked before hanging up. The
 * second half is the one that earns its place - a silently half-filled form discovered
 * after the call is worse than no form at all.
 *
 * Slots are priced by marginal insertion cost, so "Tuesday afternoon" is a
 * recommendation with a number behind it rather than a preference.
 */
export function IntakePanel() {
  const [text, setText] = useState("");
  const [result, setResult] = useState<Intake | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.intake(text));
    } catch (exc) {
      setError(exc as ApiError);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const draft = result?.draft;

  return (
    <section className="panel">
      <h2>Take a call</h2>
      <textarea
        value={text}
        rows={5}
        placeholder="Type while they talk — name, number, what's broken, when they're free…"
        onChange={(e) => setText(e.target.value)}
      />
      <button className="primary" disabled={busy || !text.trim()} onClick={run}>
        {busy ? "Reading…" : "Extract"}
      </button>

      {error && (
        <p className="error">
          {error.message}
          {error.remedy && <span className="muted"> — {error.remedy}</span>}
        </p>
      )}

      {draft && (
        <>
          <dl className="fields">
            <dt>Name</dt><dd>{draft.customer_name || <em>missing</em>}</dd>
            <dt>Phone</dt><dd>{draft.phone || <em>missing</em>}</dd>
            <dt>Address</dt><dd>{draft.address || <em>missing</em>}</dd>
            <dt>Work</dt><dd>{draft.service_type || <em>unclear</em>}</dd>
            {draft.duration_minutes > 0 && (
              <>
                <dt>Duration</dt>
                <dd>
                  {draft.duration_minutes} min ±{draft.duration_confidence}
                  <span className="muted"> from the catalogue</span>
                </dd>
                <dt>Crew</dt>
                <dd>{draft.crew_size} · {draft.certifications.join(", ") || "no certs"}</dd>
              </>
            )}
            {draft.lead_time_days > 0 && (
              <>
                <dt>Glass</dt>
                <dd className="warn">made to order, {draft.lead_time_days} day lead</dd>
              </>
            )}
          </dl>

          {draft.commitment_cost > 0 && (
            <div className="commitment">
              <strong>${draft.commitment_cost.toFixed(0)}</strong> to move this slot
              {draft.commitment_quotes.map((quote) => (
                <p key={quote} className="quote">“{quote}”</p>
              ))}
            </div>
          )}

          {result.ask_next.length > 0 && (
            <div className="ask">
              <h3>Still to ask</h3>
              <ul>{result.ask_next.map((q) => <li key={q}>{q}</li>)}</ul>
            </div>
          )}

          {result.slots.length > 0 && (
            <div className="slots">
              <h3>Offer them</h3>
              {result.slots.map((slot, index) => (
                <div key={slot.date + slot.window} className={index === 0 ? "slot slot--best" : "slot"}>
                  <div className="slot__when">{slot.window}</div>
                  <div className="slot__cost">${slot.marginal_cost.toFixed(2)}</div>
                  <div className="slot__why muted">{slot.reason} · {slot.crew}</div>
                </div>
              ))}
              {result.slots.length > 1 && (
                <p className="muted">
                  Booking the first rather than the last saves $
                  {(
                    result.slots[result.slots.length - 1]!.marginal_cost -
                    result.slots[0]!.marginal_cost
                  ).toFixed(2)}
                  .
                </p>
              )}
            </div>
          )}

          {result.repairs > 0 && (
            <p className="muted">The model needed {result.repairs} repair attempt(s).</p>
          )}
          {result.note && <p className="warn">{result.note}</p>}
        </>
      )}
    </section>
  );
}
