import type { Plan, World } from "../types";

/**
 * Who is available, and what the week is doing to them.
 *
 * Two things that were in different places and answer the same question. The roster
 * says who could work; the day breakdown says how hard each crew is actually being
 * worked, and whether anyone is sitting in a van waiting. Reading one without the
 * other means scrolling to the bottom of the calendar and back.
 *
 * The breakdown used to live under the grid, which is the wrong end of the page: it
 * is context you want before you look at the week, not a footnote after it.
 */
export function TodayPanel({ world, plan }: { world: World; plan: Plan | null }) {
  const byDate = new Map<string, Plan["routes"]>();
  for (const route of plan?.routes ?? []) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }
  const dates = [...byDate.keys()].sort();

  return (
    <section className="panel panel--today">
      <h2>Crews</h2>

      <ul className="roster">
        {world.workers.map((worker) => (
          <li key={worker.id} className={worker.available ? "" : "warn"}>
            <strong>{worker.name}</strong> <span className="muted">{worker.shift}</span>
            {!worker.available && " - unavailable"}
          </li>
        ))}
        {world.vans.map((van) => (
          <li key={van.id} className={van.available ? "muted" : "warn"}>
            {van.id}
            {!van.available && " - out of service"}
          </li>
        ))}
      </ul>

      {dates.length > 0 && (
        /* Its own ground inside the panel, because it answers a different question
           from the list above it and the two ran together as one grey wall. */
        <div className="workload">
          <h3>How the week loads them</h3>
          {dates.map((date) => (
            <div key={date} className="workload__day">
              <span className="workload__dow">
                {new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { weekday: "short" })}
              </span>
              <ul>
                {(byDate.get(date) ?? []).map((route) => (
                  <li key={route.crew_id}>
                    <span className="workload__who">
                      {route.worker_names.join(" + ")}
                      <span className="muted"> {route.van_id}</span>
                    </span>
                    <span className="workload__stats">
                      <strong>{Math.round(route.utilization * 100)}%</strong> on site
                      <span className="muted"> · {route.travel_minutes}m driving</span>
                      {/* Idle is the number that makes a technically valid plan look
                          obviously wrong, so it is the one allowed to shout. */}
                      {route.idle_minutes > 0 && (
                        <span className="warn"> · {route.idle_minutes}m idle</span>
                      )}
                      {route.overtime_minutes > 0 && (
                        <span className="warn"> · OT {route.overtime_minutes}m</span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
