import { useCallback, useEffect, useState } from "react";
import { ApiError, api, subscribe } from "./api";
import { DisruptionPanel } from "./components/DisruptionPanel";
import { Gantt } from "./components/Gantt";
import { IntakePanel } from "./components/IntakePanel";
import { ProposalPanel } from "./components/ProposalPanel";
import { RouteMap } from "./components/RouteMap";
import type { Plan, World } from "./types";

type View = "board" | "map";

export default function App() {
  const [world, setWorld] = useState<World | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [view, setView] = useState<View>("board");
  const [day, setDay] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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

      {world?.calibration_warning && (
        // Every cost on this screen rests on numbers nobody has validated. That
        // belongs in front of the reader, not in a config file they will never open.
        <div className="banner">{world.calibration_warning}</div>
      )}
      {error && (
        <div className="banner banner--error">
          {error.message}
          {error.remedy && <span className="muted"> - {error.remedy}</span>}
        </div>
      )}

      <div className="layout">
        <aside className="sidebar">
          <IntakePanel />
          <DisruptionPanel onChanged={() => void refresh()} />
        </aside>

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
          {plan && view === "board" && <Gantt plan={plan} onSelect={() => undefined} />}
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

        <aside className="sidebar">
          <ProposalPanel onChanged={() => void refresh()} />
          {world && (
            <section className="panel">
              <h2>Today</h2>
              <ul className="roster">
                {world.workers.map((w) => (
                  <li key={w.id} className={w.available ? "" : "warn"}>
                    <strong>{w.name}</strong> <span className="muted">{w.shift}</span>
                    {!w.available && " - unavailable"}
                  </li>
                ))}
                {world.vans.map((v) => (
                  <li key={v.id} className={v.available ? "muted" : "warn"}>
                    {v.id}
                    {!v.available && " - out of service"}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}
