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

/** 12-hour, because that is what this business says out loud. "Alex 8 to 17" makes a
 * dispatcher translate before they can use it, and translating is what a display is
 * supposed to have done already. */
const clock = (minute: number) => {
  const hour24 = Math.floor(minute / 60);
  const hour = hour24 % 12 || 12;
  return `${hour}:${String(minute % 60).padStart(2, "0")} ${hour24 < 12 ? "AM" : "PM"}`;
};

/** The same, without the meridiem - for the hour rail, where AM/PM on every line is
 * noise and the column reads top to bottom anyway. */
const hourLabel = (minute: number) => {
  const hour24 = Math.floor(minute / 60);
  return `${hour24 % 12 || 12} ${hour24 < 12 ? "am" : "pm"}`;
};

/** Local calendar date, not UTC - `toISOString` shifts the day east of UTC+12. */
function isoDate(value: Date): string {
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, "0"),
    String(value.getDate()).padStart(2, "0"),
  ].join("-");
}

/** Every date in the horizon, not only the ones with work on them. */
function horizonDates(from: string, to: string): string[] {
  const dates: string[] = [];
  const end = new Date(`${to}T12:00:00`);
  for (
    let cursor = new Date(`${from}T12:00:00`);
    cursor <= end;
    cursor.setDate(cursor.getDate() + 1)
  ) {
    dates.push(isoDate(cursor));
  }
  return dates;
}

/**
 * The grid spans the business's working day - 6 AM to 9 PM covers every shift, the
 * overtime reach and the pre-opening commercial starts - and widens itself if a stop
 * ever lands outside that. It renders at full height on a page that scrolls
 * naturally: an inner scrollbar on the one thing you look at all day read as a
 * patch, and "is the morning free?" is answered by looking, not by scrolling a
 * frame inside a frame.
 */
const GRID_START = 6 * 60;
const GRID_END = 21 * 60;

/** Clamp the grid to the day's actual work when it spills past the defaults. */
function gridBounds(routes: Route[]): [number, number] {
  let start = GRID_START;
  let end = GRID_END;
  for (const route of routes) {
    for (const stop of route.stops) {
      start = Math.min(start, Math.floor(stop.start_minute / 60) * 60);
      end = Math.max(end, Math.ceil(stop.end_minute / 60) * 60);
    }
  }
  return [start, end];
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
      className={`block block--${stop.commitment_state}${stop.past_shift ? " block--ot" : ""}${selected ? " block--selected" : ""}`}
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
        `${stop.travel_minutes} min drive, ${stop.travel_miles} mi` +
        (stop.past_shift ? "\nruns past shift - overtime" : "")
      }
    >
      {stop.past_shift && <span className="block__ot-tag">OT</span>}
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
  week,
  selected,
  onSelect,
}: {
  /** Null before anything is committed. An empty diary still has a week in it, and a
   * blank panel is the least useful thing to show somebody whose diary is empty. */
  plan: Plan | null;
  week: { start: string; end: string };
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const routes = plan?.routes ?? [];
  const dates = horizonDates(plan?.horizon_start ?? week.start, plan?.horizon_end ?? week.end);
  const [start, end] = gridBounds(routes);
  const hours: number[] = [];
  for (let h = Math.ceil(start / 60) * 60; h <= end; h += 60) hours.push(h);

  const byDate = new Map<string, Route[]>();
  for (const route of routes) {
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
              {hourLabel(h)}
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

      {routes.length === 0 && (
        <p className="cal__empty">
          Nothing booked this week. Take a call on the left, or press “Plan the week”.
        </p>
      )}
    </div>
  );
}
