import { useState } from "react";
import { ApiError, api } from "../api";
import type { Note } from "../types";

/** The board speaks 12-hour; the API speaks unambiguous. Converting here keeps the
 * wire format machine-readable and the screen human-readable, rather than asking one
 * of them to compromise. */
function to24h(label: string): string {
  const [, rawHour = "0", minute = "00", meridiem = ""] =
    label.trim().match(/^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$/) ?? [];
  let hour = Number(rawHour) % 12;
  if (meridiem.toLowerCase() === "pm") hour += 12;
  return `${String(hour).padStart(2, "0")}:${minute}`;
}

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
function Field({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value || <em>{hint ?? "needed"}</em>}</dd>
    </>
  );
}

export function NotePanel({ onChanged }: { onChanged: () => void }) {
  const [text, setText] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const [booked, setBooked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function run(kind?: "booking" | "disruption") {
    setBusy(true);
    setError(null);
    setBooked(null);
    try {
      setNote(await api.note(text, kind));
    } catch (exc) {
      setError(exc as ApiError);
      setNote(null);
    } finally {
      setBusy(false);
    }
  }

  async function take(date: string, arrival: string) {
    if (!draft) return;
    setBusy(true);
    try {
      const result = await api.book(draft, date, to24h(arrival));
      // Say so. The button worked before this and looked like it had not, which is
      // the worst thing a button can do: the next thing anybody does is press it again.
      setBooked(`Booked ${result.customer} for ${result.when} - ${result.status}.`);
      setNote(null);
      setText("");
      onChanged();
    } catch (exc) {
      setError(exc as ApiError);
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

      {booked && <p className="booked">{booked}</p>}

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
          {/* The non-negotiables, one fixed row each, whether heard or still needed.
              The layout does not reflow as fields fill in, which is what makes it
              readable at a glance mid-call - and the same slots hold when the source
              is an email or a transcript instead of typing. */}
          <dl className="fields">
            <Field label="Name" value={draft.customer_name} />
            <Field label="Phone" value={draft.phone} />
            <Field label="Address" value={draft.address} />
            <Field label="Work" value={draft.service_type.replace(/_/g, " ")} />
            <Field label="When" value={booking.when_text} hint="any time - worth asking" />
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
              {/* This is not a fee, a collision, or anything the customer pays. It is
                  how the planner remembers a promise has a person attached: when some
                  future disruption forces a choice about who to move, this appointment
                  looks $N more expensive to touch than one where nobody arranged
                  anything. Only shown when the caller actually said so - the quote is
                  the evidence, checked against the transcript. */}
              <strong>Worth protecting.</strong> They said:
              {draft.commitment_quotes.map((quote) => (
                <p key={quote} className="quote">“{quote}”</p>
              ))}
              <p className="commitment__why muted">
                So if a breakdown later forces us to move someone, the planner will move
                this appointment last - it treats rescheduling them as costing $
                {draft.commitment_cost.toFixed(0)} of goodwill, not as free.
              </p>
            </div>
          )}

          {booking.ask_next.length > 0 && (
            <div className="ask">
              <h3>Still to ask</h3>
              <ul>{booking.ask_next.map((q) => <li key={q}>{q}</li>)}</ul>
            </div>
          )}

          {booking.slots.length === 0 && booking.unavailable.length > 0 && (
            <div className="ask">
              <h3>Cannot offer any of these</h3>
              <ul>
                {booking.unavailable.map((u) => (
                  <li key={u.day}>
                    <strong>{u.day}</strong> - {u.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {booking.slots.length > 0 && (
            <div className="slots">
              <h3>Offer them</h3>
              {/* The list used to show a date, a window and a number, and a dispatcher
                  had to guess what the number meant and had no way to act on it. It
                  says what the money is, separates the estimate from the promise, and
                  books. */}
              <p className="slots__how">
                Best first. The big number is <em>what the customer pays</em>. Underneath
                is what we keep once the glass, the wages and the driving are paid for -
                which differs by day, because the driving does.
              </p>
              {booking.slots.map((slot, index) => (
                <div
                  key={slot.date + slot.window + slot.arrival}
                  className={`slot${index === 0 ? " slot--best" : ""}${slot.outside_preference ? " slot--flex" : ""}`}
                >
                  {slot.outside_preference && (
                    <div className="slot__flex">
                      outside their stated hours - worth floating: saves them $
                      {(
                        Math.min(
                          ...booking.slots
                            .filter((other) => !other.outside_preference)
                            .map((other) => other.quote_total),
                        ) - slot.quote_total
                      ).toFixed(2)}
                    </div>
                  )}
                  <div className="slot__day">{slot.day}</div>
                  <div className="slot__cost" title="what the customer pays, tax included">
                    ${slot.quote_total.toFixed(2)}
                  </div>
                  <div className="slot__arrival">
                    arrive about <strong>{slot.arrival}</strong>
                    <span className="muted"> · promise {slot.window}</span>
                  </div>
                  <div className="slot__margin">
                    keeps <strong>${slot.margin.toFixed(2)}</strong>
                    <span className="muted"> ({slot.margin_pct.toFixed(0)}%) after glass, wages and driving</span>
                  </div>
                  <details className="slot__breakdown">
                    <summary>how that price is built</summary>
                    <pre>{slot.quote_lines.join("\n")}</pre>
                  </details>
                  <div className="slot__why muted">
                    {slot.reason}
                    <br />
                    {slot.crew} - {slot.crew_reason}
                  </div>
                  <button
                    className="primary slot__book"
                    disabled={busy}
                    onClick={() => void take(slot.date, slot.arrival)}
                  >
                    Book it
                  </button>
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
