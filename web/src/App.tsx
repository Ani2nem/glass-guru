import { useCallback, useEffect, useState } from "react";
import { ApiError, api, subscribe } from "./api";
import { Calendar } from "./components/Calendar";
import { NotePanel } from "./components/NotePanel";
import { ProposalPanel } from "./components/ProposalPanel";
import { TodayPanel } from "./components/TodayPanel";
import { RouteMap } from "./components/RouteMap";
import type { Plan, World } from "./types";

type View = "board" | "map";

export default function App() {
  const [world, setWorld] = useState<World | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [view, setView] = useState<View>("board");
  const [day, setDay] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextWorld, nextPlan] = await Promise.all([api.world(), api.plan()]);
      setWorld(nextWorld);
      setPlan(nextPlan);
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
          <div className="topbar__plan">
            <code>{plan.plan_id}</code>
            <span className="muted">{plan.content_hash}</span>
            <span>${plan.cost.total.toFixed(2)}</span>
            <span className={plan.feasible ? "ok" : "error"}>
              {plan.feasible ? "invariants pass" : `${plan.violations.length} violation(s)`}
            </span>
          </div>
        ) : (
          <span className="muted">nothing committed</span>
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
            {busy ? "Solving…" : plan ? "Re-plan" : "Plan the week"}
          </button>
        </div>
      </header>

      {error && (
        <div className="banner banner--error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </div>
      )}

      {/* Panels across the top, board underneath at the full width of the window.
          A five-day horizon on four crews is a wide thing; squeezing it between two
          sidebars left every bar too narrow to read the customer's name in. */}
      <div className="layout">
        <section className="workbench">
          <div className="stack">
            <NotePanel onChanged={() => void refresh()} />
            {/* Directly under the note, because that is what produces it: something
                goes wrong, and these are the ways out of it. */}
            <ProposalPanel onChanged={() => void refresh()} />
          </div>
          {world && <TodayPanel world={world} plan={plan} />}
        </section>

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

          {!plan && <p className="muted">No plan yet. Press “Plan the week”.</p>}
          {plan && view === "board" && (
            <>
              {selectedStop && (
                <div className="detail">
                  <header>
                    <h3>{selectedStop.customer_name}</h3>
                    <span className={`blast blast--${selectedStop.commitment_state}`}>
                      {selectedStop.commitment_state}
                    </span>
                    <button style={{ marginLeft: "auto" }} onClick={() => setSelected(null)}>
                      Close
                    </button>
                  </header>
                  <dl>
                    <dt>Work</dt>
                    <dd>{selectedStop.service_type.replace(/_/g, " ")}</dd>
                    <dt>On site</dt>
                    <dd>
                      {selectedStop.arrival.slice(11, 16)} to {selectedStop.departure.slice(11, 16)}
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
              <Calendar plan={plan} selected={selected} onSelect={setSelected} />
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

      </div>
    </div>
  );
}
