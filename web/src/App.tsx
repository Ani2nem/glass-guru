import { useCallback, useEffect, useState } from "react";
import { ApiError, api, subscribe } from "./api";
import { Calendar } from "./components/Calendar";
import { NotePanel } from "./components/NotePanel";
import { TodayPanel } from "./components/TodayPanel";
import { RouteMap } from "./components/RouteMap";
import type { Plan, Week, World } from "./types";

type View = "board" | "map";

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
  const [error, setError] = useState<ApiError | null>(null);

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
        <h1>Glass Guru</h1>
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
          <button
            className="primary"
            disabled={busy}
            title="Rebuild the whole week's schedule from everything recorded - bookings, absences, breakdowns - and commit it. Safe to press any time; promised windows are kept wherever possible."
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
            {busy ? "Solving…" : plan ? "Re-plan the week" : "Plan the week"}
          </button>
        </div>
      </header>

      {plan && !plan.feasible && (
        /* "1 violation(s)" is a true statement that tells a dispatcher nothing. What
           they need is what broke and what to do about it, which is exactly what
           recording a disruption produces: the committed plan still sends a van out
           that is off the road. */
        <div className="banner banner--stale">
          <strong>The committed plan no longer works.</strong>{" "}
          Something recorded since it was made contradicts it:
          <ul>
            {plan.violations.map((violation) => (
              <li key={violation}>{readable(violation)}</li>
            ))}
          </ul>
          Press <strong>Re-plan the week</strong> to rebuild around it.
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
        <main className="main">
          <div className="tabs">
            <button className={view === "board" ? "on" : ""} onClick={() => setView("board")}>
              Board
            </button>
            <button className={view === "map" ? "on" : ""} onClick={() => setView("map")}>
              Map
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
                <div className="detail detail--overlay">
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
                      Reschedule
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
                    <dt>On site</dt>
                    <dd>
                      {twelveHour(selectedStop.arrival)} to {twelveHour(selectedStop.departure)}
                    </dd>
                    <dt>Drive there</dt>
                    <dd>
                      {selectedStop.travel_minutes} min · {selectedStop.travel_miles} mi
                    </dd>
                    <dt>Crew</dt>
                    <dd>{selectedStop.crew_size === 1 ? "one fitter" : `${selectedStop.crew_size} fitters`}</dd>
                    <dt>Job</dt>
                    <dd><code>{selectedStop.job_id}</code></dd>
                  </dl>
                </div>
              )}
              <Calendar plan={plan} week={week} selected={selected} onSelect={setSelected} />
            </>
          )}
          {plan && view === "map" && <RouteMap plan={plan} day={day} />}

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

        {world && <TodayPanel world={world} plan={plan} onChanged={() => void refresh()} />}
        </div>
      </div>

      {/* Somewhere to land. The quiet reference facts live here - where every route
          starts, what the calendar's colours mean, which plan is on screen - instead
          of crowding the surfaces people actually work on. */}
      <footer className="footer">
        <span className="footer__brand">Glass Guru</span>
        {world?.depot_address && <span>routes start and end at {world.depot_address}</span>}
        <div className="footer__legend">
          <span>
            <i className="footer__swatch footer__swatch--provisional" /> scheduled, not yet promised
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
