import type { Plan, Route, Stop } from "../types";

/**
 * The week, as a calendar: days across, time down.
 *
 * This replaces a horizontal Gantt, and the reason is the label problem rather than
 * taste. Laid out horizontally, a forty-five minute job is a forty-five pixel sliver
 * and "Rodriguez Storefront" cannot be written in it at any font size. I tried
 * scrolling the text on hover, which was worse: motion to read something that should
 * simply have been legible.
 *
 * Turned on its side the same job is a block forty-five pixels *tall* and the full
 * width of a day column, which is room for the customer, the time and the crew
 * without truncating any of them. The constraint was never the amount of text.
 *
 * Overlapping work inside one day splits the column, the way every calendar does it -
 * so two crews out at the same time sit side by side rather than on top of each other.
 */

//: Pixels per minute. Sets how tall the grid is, and therefore how much room the
//: shortest job has for its label. At 1.1 a forty-five minute job clears two lines.
const SCALE = 1.1;

const clock = (minute: number) =>
  `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;

/** Local calendar date, not UTC - `toISOString` shifts the day east of UTC+12. */
function isoDate(value: Date): string {
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, "0"),
    String(value.getDate()).padStart(2, "0"),
  ].join("-");
}

/** Every date in the horizon, not only the ones with work on them. */
function horizonDates(plan: Plan): string[] {
  const dates: string[] = [];
  const end = new Date(`${plan.horizon_end}T12:00:00`);
  for (
    let cursor = new Date(`${plan.horizon_start}T12:00:00`);
    cursor <= end;
    cursor.setDate(cursor.getDate() + 1)
  ) {
    dates.push(isoDate(cursor));
  }
  return dates;
}

/**
 * The window the grid draws, taken from the work rather than fixed.
 *
 * A hardcoded 06:00-19:00 either wastes a third of the screen on hours nobody works
 * or clips a crew who started at 05:30 for a pre-opening storefront job. Rounded out
 * to whole hours so the lines land somewhere sensible.
 */
function timeWindow(plan: Plan): [number, number] {
  const stops = plan.routes.flatMap((r) => r.stops);
  if (stops.length === 0) return [7 * 60, 17 * 60];
  const first = Math.min(...stops.map((s) => s.start_minute));
  const last = Math.max(...stops.map((s) => s.end_minute));
  return [Math.floor(first / 60) * 60 - 30, Math.ceil(last / 60) * 60 + 30];
}

interface Placed {
  stop: Stop;
  route: Route;
  column: number;
  columns: number;
}

/**
 * Lay a day's stops out in columns so nothing overlaps anything else.
 *
 * Events are grouped into clusters of work that actually overlaps, and only a cluster
 * splits. The first version split the whole day by its busiest moment, so three crews
 * out at nine in the morning made every block that day a third of a column wide -
 * including the lone afternoon job with nothing near it, which then had no room for
 * its label again. That is the bug this layout exists to avoid, reintroduced one
 * level down.
 */
function layout(routes: Route[]): Placed[] {
  const all = routes
    .flatMap((route) => route.stops.map((stop) => ({ stop, route })))
    .sort((a, b) => a.stop.start_minute - b.stop.start_minute);

  const out: Placed[] = [];
  let cluster: { stop: Stop; route: Route }[] = [];
  let clusterEnd = -Infinity;

  function flush() {
    if (cluster.length === 0) return;
    const ends: number[] = [];
    const placed = cluster.map(({ stop, route }) => {
      let column = ends.findIndex((end) => end <= stop.start_minute);
      if (column === -1) {
        column = ends.length;
        ends.push(stop.end_minute);
      } else {
        ends[column] = stop.end_minute;
      }
      return { stop, route, column };
    });
    const columns = Math.max(1, ends.length);
    out.push(...placed.map((p) => ({ ...p, columns })));
    cluster = [];
    clusterEnd = -Infinity;
  }

  for (const item of all) {
    // A gap with nothing running across it ends the cluster, so what follows starts
    // again at full width.
    if (cluster.length > 0 && item.stop.start_minute >= clusterEnd) flush();
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.stop.end_minute);
  }
  flush();
  return out;
}

function Block({
  placed,
  top,
  selected,
  onSelect,
}: {
  placed: Placed;
  top: number;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const { stop, route, column, columns } = placed;
  const height = Math.max(26, (stop.end_minute - stop.start_minute) * SCALE);
  const width = 100 / columns;
  const crew = route.worker_names.join(" + ");

  return (
    <button
      className={`block block--${stop.commitment_state}${selected ? " block--selected" : ""}`}
      style={{
        top: `${(stop.start_minute - top) * SCALE}px`,
        height: `${height}px`,
        left: `calc(${column * width}% + 2px)`,
        width: `calc(${width}% - 4px)`,
      }}
      onClick={() => onSelect(stop.job_id)}
      title={
        `${stop.customer_name} - ${stop.service_type.replace(/_/g, " ")}\n` +
        `${clock(stop.start_minute)}-${clock(stop.end_minute)}\n` +
        `${crew} · ${route.van_id}\n` +
        `${stop.travel_minutes} min drive, ${stop.travel_miles} mi`
      }
    >
      <span className="block__what">{stop.customer_name}</span>
      {/* The shortest job on the fixture is forty-five minutes, which is fifty pixels
          tall and just holds a wrapped name plus the time. Below that the time goes
          first: knowing who it is matters more than knowing to the minute when, and
          both are in the tooltip and the detail panel regardless. */}
      {height > 44 && (
        <span className="block__when">
          {clock(stop.start_minute)} - {clock(stop.end_minute)}
        </span>
      )}
      {/* Only shown when the block is tall enough to hold it. A short job says who is
          on it in the tooltip and in the detail panel instead of squeezing three
          lines into two lines of space. */}
      {height > 58 && <span className="block__who">{crew}</span>}
    </button>
  );
}

export function Calendar({
  plan,
  selected,
  onSelect,
}: {
  plan: Plan;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const dates = horizonDates(plan);
  const [start, end] = timeWindow(plan);
  const hours: number[] = [];
  for (let h = Math.ceil(start / 60) * 60; h <= end; h += 60) hours.push(h);

  const byDate = new Map<string, Route[]>();
  for (const route of plan.routes) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }

  return (
    <div className="cal">
      <div className="cal__days">
        <div className="cal__corner" />
        {dates.map((date) => {
          const routes = byDate.get(date) ?? [];
          const jobs = routes.reduce((n, r) => n + r.stops.length, 0);
          const day = new Date(`${date}T12:00:00`);
          return (
            <div key={date} className={`cal__day${jobs === 0 ? " cal__day--free" : ""}`}>
              <span className="cal__dow">
                {day.toLocaleDateString(undefined, { weekday: "short" })}
              </span>
              <strong className="cal__date">{day.getDate()}</strong>
              <span className="cal__count">
                {jobs === 0 ? "free" : `${jobs} job${jobs === 1 ? "" : "s"}`}
              </span>
            </div>
          );
        })}
      </div>

      <div className="cal__grid" style={{ height: `${(end - start) * SCALE}px` }}>
        <div className="cal__gutter">
          {hours.map((h) => (
            <span key={h} style={{ top: `${(h - start) * SCALE}px` }}>
              {clock(h)}
            </span>
          ))}
        </div>

        {dates.map((date) => (
          <div key={date} className="cal__column">
            {hours.map((h) => (
              <div key={h} className="cal__line" style={{ top: `${(h - start) * SCALE}px` }} />
            ))}
            {layout(byDate.get(date) ?? []).map((placed) => (
              <Block
                key={placed.stop.job_id}
                placed={placed}
                top={start}
                selected={selected === placed.stop.job_id}
                onSelect={onSelect}
              />
            ))}
          </div>
        ))}
      </div>

      {/* Utilisation, driving and idle time per crew. The calendar answers "what does
          Wednesday look like"; this answers "is anybody sitting around", which the
          old row-per-crew layout showed for free and a day-per-column one does not. */}
      <div className="cal__crews">
        {dates.map((date) => {
          const routes = byDate.get(date) ?? [];
          if (routes.length === 0) return null;
          return (
            <div key={date} className="cal__crewday">
              <span className="muted">
                {new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { weekday: "short" })}
              </span>
              {routes.map((route) => (
                <span key={route.crew_id} className="cal__crew">
                  <strong>{route.worker_names.join(" + ")}</strong>
                  <span className="muted"> {route.van_id}</span>
                  {" · "}
                  {Math.round(route.utilization * 100)}% on site
                  {" · "}
                  {route.travel_minutes}m driving
                  {route.idle_minutes > 0 && <span className="warn"> · {route.idle_minutes}m idle</span>}
                  {route.overtime_minutes > 0 && (
                    <span className="warn"> · OT {route.overtime_minutes}m</span>
                  )}
                </span>
              ))}
            </div>
          );
        })}
      </div>

      {plan.routes.length === 0 && <p className="muted">Nothing scheduled.</p>}
    </div>
  );
}
