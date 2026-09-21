import { useState } from "react";
import { ApiError, api } from "../api";
import type { Note } from "../types";

/**
 * One box. Type what you just heard.
 *
 * There used to be two, side by side and identical: one booked jobs, one recorded
 * disruptions. The first person to use the board typed "Dan called, van 3 won't
 * start" into the booking one and got back a customer named Dan who wanted auto glass
 * fitted. Better labels would not have fixed that - a dispatcher writing down a phone
 * call should not have to know which of two agents wants it.
 *
 * So a classifier reads the note first and hands it to the right specialist. It says
 * which it chose and why, and one click overrides it, which is what makes routing by
 * model safe here: the worst case costs a click rather than a wrong job on the
 * schedule.
 */
export function NotePanel({ onChanged }: { onChanged: () => void }) {
  const [text, setText] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function run(kind?: "booking" | "disruption") {
    setBusy(true);
    setError(null);
    try {
      setNote(await api.note(text, kind));
    } catch (exc) {
      setError(exc as ApiError);
      setNote(null);
    } finally {
      setBusy(false);
    }
  }

  async function accept() {
    const events = note?.disruption?.events;
    if (!events?.length) return;
    setBusy(true);
    try {
      await api.acceptTriage(events);
      setNote(null);
      setText("");
      onChanged();
    } catch (exc) {
      setError(exc as ApiError);
    } finally {
      setBusy(false);
    }
  }

  const draft = note?.booking?.draft;
  const booking = note?.booking;
  const disruption = note?.disruption;
  const other = note?.kind === "disruption" ? "booking" : "disruption";

  return (
    <section className={`panel panel--note${note ? ` panel--${note.kind}` : ""}`}>
      <h2>What happened</h2>
      <p className="panel__hint">
        A customer calling, or something going wrong. Type it either way.
      </p>
      <textarea
        value={text}
        rows={4}
        placeholder="Maria at Nguyen Glass, storefront pane smashed… / Dan called, van 3 won't start…"
        onChange={(e) => setText(e.target.value)}
      />
      <button className="primary" disabled={busy || !text.trim()} onClick={() => void run()}>
        {busy ? "Reading…" : "Read it"}
      </button>

      {error && (
        <p className="error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </p>
      )}

      {note && (
        <div className="routed">
          <span className={`tag tag--${note.kind}`}>
            {note.kind === "booking" ? "new booking" : "disruption"}
          </span>
          <span className="muted">{note.why}</span>
          {/* The override. Without it, a misrouted note is a dead end and the
              dispatcher retypes it into a box that no longer exists. */}
          <button
            className="link"
            disabled={busy}
            onClick={() => void run(other as "booking" | "disruption")}
          >
            not a {note.kind}?
          </button>
        </div>
      )}

      {draft && booking && (
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

          {booking.ask_next.length > 0 && (
            <div className="ask">
              <h3>Still to ask</h3>
              <ul>{booking.ask_next.map((q) => <li key={q}>{q}</li>)}</ul>
            </div>
          )}

          {booking.slots.length > 0 && (
            <div className="slots">
              <h3>Offer them</h3>
              {booking.slots.map((slot, index) => (
                <div
                  key={slot.date + slot.window}
                  className={index === 0 ? "slot slot--best" : "slot"}
                >
                  <div className="slot__when">{slot.window}</div>
                  <div className="slot__cost">${slot.marginal_cost.toFixed(2)}</div>
                  <div className="slot__why muted">{slot.reason} · {slot.crew}</div>
                </div>
              ))}
              {booking.slots.length > 1 && (
                <p className="muted">
                  Booking the first rather than the last saves $
                  {(
                    booking.slots[booking.slots.length - 1]!.marginal_cost -
                    booking.slots[0]!.marginal_cost
                  ).toFixed(2)}
                  .
                </p>
              )}
            </div>
          )}

          {booking.repairs > 0 && (
            <p className="muted">The model needed {booking.repairs} repair attempt(s).</p>
          )}
          {booking.note && <p className="warn">{booking.note}</p>}
        </>
      )}

      {disruption && (
        <>
          {disruption.summary && <p>{disruption.summary}</p>}
          {disruption.events.length > 0 && (
            <ul className="events">
              {disruption.events.map((event, index) => (
                <li key={index}>
                  <code>{String(event.type)}</code>{" "}
                  {String(event.van_id ?? event.worker_id ?? event.job_id ?? "")}
                  {event.reason ? <span className="muted"> - {String(event.reason)}</span> : null}
                </li>
              ))}
            </ul>
          )}
          {disruption.question && <p className="warn">{disruption.question}</p>}
          {disruption.rejected.map((line) => (
            <p key={line} className="warn">{line}</p>
          ))}
          {disruption.repairs > 0 && (
            <p className="muted">The model needed {disruption.repairs} repair attempt(s).</p>
          )}
          {/* Nothing the agent extracted is recorded until a dispatcher agrees. An
              event is a fact in an append-only log, and a wrong one propagates into
              every plan that follows. */}
          {disruption.events.length > 0 && (
            <button className="primary" disabled={busy} onClick={() => void accept()}>
              Record {disruption.events.length} event(s)
            </button>
          )}
        </>
      )}
    </section>
  );
}
