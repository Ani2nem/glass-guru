import type { Plan, Route, Stop } from "../types";

// The working day the board draws. Crews start at 06:00 for pre-opening storefront
// work and can run into overtime, so the window is wider than a nominal shift.
const DAY_START = 5 * 60;
const DAY_END = 19 * 60;
const SPAN = DAY_END - DAY_START;

// Clamped: a route pushed into overtime would otherwise draw outside its own track
// and overlap the row beneath, which reads as a scheduling bug rather than a
// rendering one.
const pct = (minute: number) =>
  Math.min(100, Math.max(0, ((minute - DAY_START) / SPAN) * 100));
const clock = (minute: number) =>
  `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;

function StopBar({ stop, onSelect }: { stop: Stop; onSelect: (id: string) => void }) {
  const left = pct(stop.start_minute);
  const width = Math.max(1.2, Math.min(100 - left, pct(stop.end_minute) - left));
  const clipped = stop.end_minute > DAY_END || stop.start_minute < DAY_START;
  // Commitment state drives colour because "can this move?" is the dispatcher's
  // constant question, and it is answered by state rather than by time.
  const tone = stop.commitment_state;
  return (
    <button
      className={`bar bar--${tone}${clipped ? " bar--clipped" : ""}`}
      style={{ left: `${left}%`, width: `${width}%` }}
      onClick={() => onSelect(stop.job_id)}
      title={
        `${stop.customer_name} - ${stop.service_type}\n` +
        `${clock(stop.start_minute)}–${clock(stop.end_minute)}\n` +
        `${stop.travel_minutes} min drive, ${stop.travel_miles} mi\n` +
        `${stop.commitment_state}${stop.crew_size > 1 ? ` · needs ${stop.crew_size}` : ""}`
      }
    >
      <span className="bar__label">{stop.customer_name}</span>
    </button>
  );
}

function RouteRow({ route, onSelect }: { route: Route; onSelect: (id: string) => void }) {
  return (
    <div className="route">
      <div className="route__who">
        <strong>{route.worker_names.join(" + ")}</strong>
        <span className="muted">{route.van_id}</span>
      </div>
      <div className="route__track">
        {route.stops.map((stop) => (
          <StopBar key={stop.job_id} stop={stop} onSelect={onSelect} />
        ))}
      </div>
      <div className="route__stats">
        <span title="time on site as a share of the working day">
          {Math.round(route.utilization * 100)}%
        </span>
        <span className="muted">{route.travel_minutes}m drive</span>
        {route.idle_minutes > 0 && (
          <span className="warn" title="slack between jobs">
            {route.idle_minutes}m idle
          </span>
        )}
        {route.overtime_minutes > 0 && <span className="warn">OT {route.overtime_minutes}m</span>}
      </div>
    </div>
  );
}

export function Gantt({ plan, onSelect }: { plan: Plan; onSelect: (id: string) => void }) {
  const byDate = new Map<string, Route[]>();
  for (const route of plan.routes) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }
  const hours = Array.from({ length: (DAY_END - DAY_START) / 60 + 1 }, (_, i) => DAY_START + i * 60);

  return (
    <div className="gantt">
      {[...byDate.entries()].sort().map(([date, routes]) => {
        const jobs = routes.reduce((n, r) => n + r.stops.length, 0);
        const driving = routes.reduce((n, r) => n + r.travel_minutes, 0);
        return (
          <section key={date} className="day">
            <header className="day__header">
              <h3>{new Date(`${date}T12:00:00`).toLocaleDateString(undefined, {
                weekday: "long", day: "numeric", month: "short",
              })}</h3>
              <span className="muted">
                {routes.length} crew · {jobs} jobs · {driving} min driving
              </span>
            </header>
            <div className="day__scale">
              {hours.map((h) => (
                <span key={h} style={{ left: `${pct(h)}%` }}>{clock(h)}</span>
              ))}
            </div>
            {routes.map((route) => (
              <RouteRow key={route.crew_id} route={route} onSelect={onSelect} />
            ))}
          </section>
        );
      })}
      {plan.routes.length === 0 && <p className="muted">Nothing scheduled.</p>}
    </div>
  );
}
