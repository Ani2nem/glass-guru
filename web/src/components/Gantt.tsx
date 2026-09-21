import { type CSSProperties, useRef, useState } from "react";

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

/** Local calendar date, not UTC.
 *
 * `toISOString().slice(0, 10)` looks equivalent and is not: it converts to UTC first,
 * so east of UTC+12 local noon is the previous day there and every date on the board
 * shifts back one. Seattle never sees it, which is exactly what makes it the kind of
 * bug that ships.
 */
function isoDate(value: Date): string {
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, "0"),
    String(value.getDate()).padStart(2, "0"),
  ].join("-");
}

const dayName = (date: string) =>
  new Date(`${date}T12:00:00`).toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "short",
  });

/**
 * Every date in the horizon, not only the ones with work on them.
 *
 * The board used to build its day list from the routes, so a day nobody was
 * scheduled on simply did not appear - the plan said Monday to Friday and four days
 * were drawn. "Is Friday free?" is a question a dispatcher asks constantly, and an
 * absent day answers it by looking like a bug.
 */
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

function StopBar({
  stop,
  selected,
  onSelect,
}: {
  stop: Stop;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  // A short job is a narrow bar, and a narrow bar cannot show "Rodriguez Storefront".
  // Hovering scrolls the label through, at a constant speed, and only when it actually
  // overflows - measured rather than assumed, because a bar that jiggles a name which
  // already fits is worse than one that does nothing.
  const label = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState(0);

  function measure() {
    const el = label.current;
    if (!el) return;
    const overflow = el.scrollWidth - el.clientWidth;
    setShift(overflow > 1 ? overflow : 0);
  }

  const left = pct(stop.start_minute);
  const width = Math.max(1.2, Math.min(100 - left, pct(stop.end_minute) - left));
  const clipped = stop.end_minute > DAY_END || stop.start_minute < DAY_START;
  // Commitment state drives colour because "can this move?" is the dispatcher's
  // constant question, and it is answered by state rather than by time.
  const tone = stop.commitment_state;
  return (
    <button
      className={`bar bar--${tone}${clipped ? " bar--clipped" : ""}${selected ? " bar--selected" : ""}`}
      style={{ left: `${left}%`, width: `${width}%` }}
      onClick={() => onSelect(stop.job_id)}
      onMouseEnter={measure}
      onFocus={measure}
      title={
        `${stop.customer_name} - ${stop.service_type}\n` +
        `${clock(stop.start_minute)}-${clock(stop.end_minute)}\n` +
        `${stop.travel_minutes} min drive, ${stop.travel_miles} mi\n` +
        `${stop.commitment_state}${stop.crew_size > 1 ? ` · needs ${stop.crew_size}` : ""}`
      }
    >
      <span
        ref={label}
        className="bar__label"
        style={
          shift > 0
            ? ({
                "--shift": `${shift}px`,
                // Constant speed rather than constant duration, so a long name does
                // not race past while a short one crawls.
                "--travel": `${Math.max(1.2, shift / 40)}s`,
              } as CSSProperties)
            : undefined
        }
      >
        {stop.customer_name}
      </span>
    </button>
  );
}

function RouteRow({
  route,
  selected,
  onSelect,
}: {
  route: Route;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const who = route.worker_names.join(" + ");
  return (
    <div className="route">
      <div className="route__who">
        {/* title as well as wrapping: two long names still deserve a way to read
            them in full without resizing the window. */}
        <strong title={who}>{who}</strong>
        <span className="muted">{route.van_id}</span>
      </div>
      <div className="route__track">
        {route.stops.map((stop) => (
          <StopBar
            key={stop.job_id}
            stop={stop}
            selected={selected === stop.job_id}
            onSelect={onSelect}
          />
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

export function Gantt({
  plan,
  selected,
  onSelect,
}: {
  plan: Plan;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const byDate = new Map<string, Route[]>();
  for (const route of plan.routes) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }
  const hours = Array.from({ length: (DAY_END - DAY_START) / 60 + 1 }, (_, i) => DAY_START + i * 60);

  return (
    <div className="gantt">
      <div className="legend">
        <span><i className="bar--provisional" /> provisional - free to move</span>
        <span><i className="bar--confirmed" /> confirmed - promised to a customer</span>
        <span><i className="bar--dispatched" /> dispatched - crew on the way</span>
        <span className="faint">click a bar for detail</span>
      </div>

      {horizonDates(plan).map((date) => {
        const routes = byDate.get(date) ?? [];
        const jobs = routes.reduce((n, r) => n + r.stops.length, 0);
        const driving = routes.reduce((n, r) => n + r.travel_minutes, 0);
        return (
          <section key={date} className={`day${routes.length === 0 ? " day--empty" : ""}`}>
            <header className="day__header">
              <h3>{dayName(date)}</h3>
              <span className="muted">
                {routes.length === 0
                  ? "no work scheduled"
                  : `${routes.length} crew · ${jobs} jobs · ${driving} min driving`}
              </span>
            </header>
            {routes.length > 0 && (
              <>
                <div className="day__scale">
                  {hours.map((h) => (
                    <span key={h} style={{ left: `${pct(h)}%` }}>{clock(h)}</span>
                  ))}
                </div>
                {routes.map((route) => (
                  <RouteRow
                    key={route.crew_id}
                    route={route}
                    selected={selected}
                    onSelect={onSelect}
                  />
                ))}
              </>
            )}
          </section>
        );
      })}

      {plan.routes.length === 0 && <p className="muted">Nothing scheduled.</p>}
    </div>
  );
}
