import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, subscribe } from "./api";
import { Calendar } from "./components/Calendar";
import { Logo } from "./components/Logo";
import { NotePanel } from "./components/NotePanel";
import { ConfigPanel } from "./components/ConfigPanel";
import { TodayPanel } from "./components/TodayPanel";
import { RouteMap } from "./components/RouteMap";
import type { Plan, Week, World } from "./types";

type View = "board" | "map" | "crew";

/**
 * A violation as a dispatcher would say it.
 *
 * The checker writes for the checker: "[van_unavailable] van is scheduled during a
 * recorded outage (crew=crew-van-3 van=van-3)". Every part of that is useful and none
 * of it is a sentence. The code in brackets is the rule that fired, which matters in
 * a test and not on a phone call, and the parenthetical is the detail that actually
 * says which van.
 */
/** "2026-10-01T16:00:00-05:00" -> "4:00 PM". Nobody here reads military time. */
function twelveHour(iso: string): string {
  const hour24 = Number(iso.slice(11, 13));
  const hour = hour24 % 12 || 12;
  return `${hour}:${iso.slice(14, 16)} ${hour24 < 12 ? "AM" : "PM"}`;
}

function readable(violation: string): string {
  const withoutCode = violation.replace(/^\[[a-z_]+\]\s*/, "");
  const [, body = withoutCode, detail = ""] = withoutCode.match(/^(.*?)\s*\((.*)\)$/) ?? [];
  const parts = detail
    .split(/\s+/)
    .filter(Boolean)
    .map((pair) => pair.replace("=", " "))
    .join(", ");
  return parts ? `${body} - ${parts}` : body;
}

export default function App() {
  const [world, setWorld] = useState<World | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [view, setView] = useState<View>("board");
  const [day, setDay] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [week, setWeek] = useState<Week | null>(null);
  const [prefill, setPrefill] = useState<{ text: string; nonce: number } | null>(null);
  const [configuring, setConfiguring] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);

  // A floating card should yield to a click anywhere else - reaching for its Close
  // button is a chore the rest of the screen can do for free. Calendar blocks are
  // excluded: clicking another job means "show me that one", not "dismiss".
  useEffect(() => {
    if (!selected) return;
    function onDown(e: MouseEvent) {
      const target = e.target as Element;
      if (detailRef.current?.contains(target)) return;
      if (target.closest(".block")) return;
      setSelected(null);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [selected]);

  const refresh = useCallback(async () => {
    try {
      const [nextWorld, nextPlan, nextWeek] = await Promise.all([
        api.world(),
        api.plan(),
        api.week(),
      ]);
      setWorld(nextWorld);
      setPlan(nextPlan);
      setWeek(nextWeek);
      setError(null);
    } catch (exc) {
      setError(exc as ApiError);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Live updates, so a change made from the CLI or by an agent shows up here too.
    return subscribe(() => void refresh());
  }, [refresh]);

  const days = [...new Set(plan?.routes.map((r) => r.date) ?? [])].sort();
  const selectedStop =
    plan?.routes.flatMap((r) => r.stops).find((s) => s.job_id === selected) ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">
          <Logo size={30} />
          <h1>Krama</h1>
        </span>
        {plan ? (
          <div
            className="topbar__plan"
            title={`version ${plan.plan_id} \u00B7 ${plan.content_hash}`}
          >
            <span className={plan.feasible ? "ok" : "error"}>
              {plan.feasible ? "schedule holds" : "schedule broken"}
            </span>
            <span
              className="muted"
              title="what the planner expects this week to cost us in wages and driving - not revenue"
            >
              runs the week for ~${Math.round(plan.cost.total)}
            </span>
          </div>
        ) : (
          <span className="muted">no schedule committed yet</span>
        )}
        <div className="topbar__actions">
          {world?.calibration_warning && (
            // Every cost on screen rests on numbers nobody has validated, and that
            // belongs in front of the reader. It does not belong across the full width
            // in warning yellow: it is a standing caveat, not an incident, and a banner
            // that never changes stops being read within a day.
            <span className="chip chip--warn" title={world.calibration_warning}>
              estimated costs
            </span>
          )}
          <button disabled={busy} onClick={() => setConfiguring(true)}>
            Configure
          </button>
        </div>
      </header>

      {plan && !plan.feasible && (
        /* "1 violation(s)" is a true statement that tells a dispatcher nothing. What
           they need is what broke and the way out - so the way out lives HERE, not
           as a standing button in the topbar. Bookings, outages and claims all
           re-plan themselves; the one moment a human presses anything is the moment
           this banner is on screen, and a button that exists at any other time is a
           button someone has to wonder about. */
        <div className="banner banner--stale">
          <strong>The committed plan no longer works.</strong>{" "}
          Something recorded since it was made contradicts it:
          <ul>
            {plan.violations.map((violation) => (
              <li key={violation}>{readable(violation)}</li>
            ))}
          </ul>
          <button
            className="primary"
            disabled={busy}
            title="Rebuild the week around what was recorded. Promised windows are kept wherever possible; one that truly cannot be kept stays visibly broken for a phone call."
            onClick={async () => {
              setBusy(true);
              try {
                setPlan(await api.commit());
              } catch (exc) {
                setError(exc as ApiError);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Solving…" : "Rebuild around it"}
          </button>
        </div>
      )}
      {error && (
        <div className="banner banner--error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </div>
      )}

      {/* The calendar is the thing a dispatcher looks at all day, so it owns the
          screen: a fixed-viewport grid, no page scroll. The call box lives in a rail
          on the left - it is a stand-in until phones and email feed this directly,
          and a stand-in does not get the hero slot. The crew rota runs as a wide
          strip under the calendar, which is the shape the table actually is. */}
      <div className="layout">
        <aside className="rail">
          <NotePanel onChanged={() => void refresh()} prefill={prefill} />
        </aside>

        <div className="boardcol">
        {world && world.crew_asks.length > 0 && (
          <section className="asks">
            {/* Unresolved promises to call a customer back. Red and persistent on
                purpose: each one is a person waiting by their phone. */}
            {world.crew_asks.map((ask) => (
              <div key={ask.ask_id} className="asks__row">
                <div className="asks__what">
                  <strong>{ask.customer || "A caller"}</strong>
                  {ask.phone && <span className="muted"> · {ask.phone}</span>}
                  <span> needs {ask.day} until {ask.until_label} - </span>
                  <span className="asks__who">
                    {ask.candidates
                      .filter((c) => !ask.extended.includes(c.name))
                      .map((c) => c.name)
                      .join(", ") || "everyone asked"}{" "}
                    {ask.extended.length === ask.candidates.length
                      ? "all said yes"
                      : "still to ask"}
                  </span>
                  {ask.extended.length > 0 && (
                    <span className="ok"> · {ask.extended.join(", ")} said yes</span>
                  )}
                </div>
                <div className="asks__actions">
                  {ask.candidates
                    .filter((c) => !ask.extended.includes(c.name))
                    .map((c) => (
                      <button
                        key={c.id}
                        disabled={busy}
                        title={`${c.name} agreed to stay until ${ask.until_label} that day`}
                        onClick={async () => {
                          setBusy(true);
                          try {
                            await api.extendAsk(ask.ask_id, c.id);
                            await refresh();
                          } catch (exc) {
                            setError(exc as ApiError);
                          } finally {
                            setBusy(false);
                          }
                        }}
                      >
                        {c.name} said yes
                      </button>
                    ))}
                  <button
                    disabled={busy}
                    title="bring the customer's words back into the call box to book them"
                    onClick={() => setPrefill({ text: ask.transcript, nonce: Date.now() })}
                  >
                    Book them
                  </button>
                  <button
                    className="danger"
                    disabled={busy}
                    title="done - booked, or the customer was told no"
                    onClick={async () => {
                      setBusy(true);
                      try {
                        await api.closeAsk(ask.ask_id, "resolved");
                        await refresh();
                      } catch (exc) {
                        setError(exc as ApiError);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    Resolve
                  </button>
                </div>
              </div>
            ))}
          </section>
        )}
        <main className="main">
          <div className="tabs">
            <button className={view === "board" ? "on" : ""} onClick={() => setView("board")}>
              Board
            </button>
            <button className={view === "map" ? "on" : ""} onClick={() => setView("map")}>
              Map
            </button>
            <button className={view === "crew" ? "on" : ""} onClick={() => setView("crew")}>
              Crew
            </button>
            {view === "map" && (
              <select value={day ?? ""} onChange={(e) => setDay(e.target.value || null)}>
                <option value="">all days</option>
                {days.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            )}
          </div>

          {week && view === "board" && (
            <>
              {selectedStop && (
                <div className="detail detail--overlay" ref={detailRef}>
                  <header>
                    <h3>{selectedStop.customer_name}</h3>
                    <span className={`blast blast--${selectedStop.commitment_state}`}>
                      {selectedStop.commitment_state}
                    </span>
                    <button
                      style={{ marginLeft: "auto" }}
                      disabled={busy}
                      onClick={async () => {
                        // A reschedule is a cancel that keeps the conversation. The
                        // original transcript goes back into the box, the dispatcher
                        // adds what changed, and the whole intake path - pricing,
                        // grounding, the lot - runs again rather than being edited
                        // around.
                        const transcript =
                          world?.jobs.find((j) => j.id === selectedStop.job_id)?.transcript ?? "";
                        setBusy(true);
                        try {
                          await api.cancel(selectedStop.job_id);
                          setPrefill({ text: transcript, nonce: Date.now() });
                          setSelected(null);
                          await refresh();
                        } catch (exc) {
                          setError(exc as ApiError);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Edit
                    </button>
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        try {
                          await api.cancel(selectedStop.job_id);
                          setSelected(null);
                          await refresh();
                        } catch (exc) {
                          setError(exc as ApiError);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Cancel this booking
                    </button>
                    <button onClick={() => setSelected(null)}>Close</button>
                  </header>
                  <dl>
                    <dt>Work</dt>
                    <dd>{selectedStop.service_type.replace(/_/g, " ")}</dd>
                    <dt>Address</dt>
                    <dd>{world?.jobs.find((j) => j.id === selectedStop.job_id)?.address || "-"}</dd>
                    <dt>Phone</dt>
                    <dd>{world?.jobs.find((j) => j.id === selectedStop.job_id)?.phone || "-"}</dd>
                    <dt>Quoted</dt>
                    <dd>
                      {(() => {
                        const total = world?.jobs.find(
                          (j) => j.id === selectedStop.job_id,
                        )?.quoted_total;
                        // Jobs seeded before prices were stored show a dash, not $0 -
                        // zero would read as "free", which nobody promised.
                        return total ? `$${total.toFixed(2)} (tax included)` : "-";
                      })()}
                    </dd>
                    <dt>On site</dt>
                    <dd>
                      {twelveHour(selectedStop.arrival)} to {twelveHour(selectedStop.departure)}
                    </dd>
                    <dt>Drive there</dt>
                    <dd>
                      {selectedStop.travel_minutes} min · {selectedStop.travel_miles} mi
                      {selectedStop.from_label && (
                        <span className="muted"> from {selectedStop.from_label}</span>
                      )}
                    </dd>
                    <dt>Why then</dt>
                    <dd>
                      {(() => {
                        const note = world?.jobs.find(
                          (j) => j.id === selectedStop.job_id,
                        )?.booking_note;
                        // Labelled as quote-time truth: a later re-plan may
                        // legitimately have chosen a different crew than the card
                        // predicted, and pretending otherwise is how trust dies.
                        return note ? `${note} (when quoted)` : "-";
                      })()}
                    </dd>
                    <dt>Crew</dt>
                    <dd>
                      {(() => {
                        // Who is actually rostered, not just how many the job needs.
                        // "one fitter" next to a calendar card naming two people read
                        // as a bug; the two are the van's crew, each aboard for their
                        // own stops.
                        const route = plan?.routes.find((r) =>
                          r.stops.some((st) => st.job_id === selectedStop.job_id),
                        );
                        const needs =
                          selectedStop.crew_size === 1 ? "needs one fitter" : `needs ${selectedStop.crew_size} fitters`;
                        return route
                          ? `${route.worker_names.join(" + ")} · ${route.van_id} (${needs})`
                          : needs;
                      })()}
                    </dd>
                    <dt>Job</dt>
                    <dd><code>{selectedStop.job_id}</code></dd>
                  </dl>
                </div>
              )}
              <Calendar plan={plan} selected={selected} onSelect={setSelected} />
            </>
          )}
          {plan && view === "map" && <RouteMap plan={plan} day={day} />}
          {world && view === "crew" && (
            <TodayPanel world={world} plan={plan} onChanged={() => void refresh()} />
          )}

          {plan && plan.unserved.length > 0 && (
            <section className="unserved">
              <h3>Unserved</h3>
              {/* Split deliberately: "could not fit" is a problem to act on,
                  "not this plan's work" is routine. */}
              {plan.unserved.filter((u) => u.is_failure).map((u) => (
                <p key={u.job_id} className="warn">
                  <strong>{u.customer_name || u.job_id}</strong> - {u.reason}: {u.detail}
                </p>
              ))}
              {plan.unserved.filter((u) => !u.is_failure).map((u) => (
                <p key={u.job_id} className="muted">
                  {u.customer_name || u.job_id} - {u.detail}
                </p>
              ))}
            </section>
          )}
        </main>

        </div>
      </div>

      {/* Somewhere to land. The quiet reference facts live here - where every route
          starts, what the calendar's colours mean, which plan is on screen - instead
          of crowding the surfaces people actually work on. */}
      {configuring && world && (
        <ConfigPanel
          world={world}
          onClose={() => setConfiguring(false)}
          onChanged={() => void refresh()}
        />
      )}

      <footer className="footer">
        <span className="footer__brand">Krama</span>
        {world?.depot_address && <span>routes start and end at {world.depot_address}</span>}
        <div className="footer__legend">
          <span>
            <i className="footer__swatch footer__swatch--provisional" /> scheduled, not yet
            promised
          </span>
          <span>
            <i className="footer__swatch footer__swatch--confirmed" /> promised to the customer
          </span>
          <span>
            <i className="footer__swatch footer__swatch--dispatched" /> crew on the way
          </span>
          <span>
            <i className="footer__swatch footer__swatch--ot" /> runs past shift (overtime)
          </span>
        </div>
        {plan && (
          <span className="footer__plan" title="plan version and content hash">
            {plan.plan_id} · {plan.content_hash.slice(0, 8)}
          </span>
        )}
      </footer>
    </div>
  );
}
