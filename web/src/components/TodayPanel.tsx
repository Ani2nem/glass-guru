import { useState } from "react";

import { api } from "../api";
import type { Plan, World } from "../types";

/**
 * The crew, as a rota rather than a list.
 *
 * Days across, one row per fitter, a symbol per skill. The cells are the controls:
 * click a day to mark that fitter out for that day, click an out day to bring them
 * back. The first version put a popup menu on the row with choices like "rest of
 * today", which answered the wrong question - absence has a date, and the dates are
 * already on the screen. The popup also opened inside the table's scroll frame,
 * where overflow clipped it invisible. Cells cannot be clipped by their own table.
 */

/** One symbol per certification, readable at a squint. The legend underneath spells
 * them out; the tooltip on each chip repeats it on hover. */
const CERT_BADGES: Record<string, { icon: string; label: string }> = {
  residential_glazing: { icon: "\u{1F3E0}", label: "residential glazing" },
  commercial_storefront: { icon: "\u{1F3E2}", label: "commercial storefront" },
  auto_glass: { icon: "\u{1F697}", label: "auto glass" },
  tempered_safety: { icon: "\u{1F6E1}\u{FE0F}", label: "tempered safety" },
  screen_repair: { icon: "\u{1FA9F}", label: "screen repair" },
  shower_door: { icon: "\u{1F6BF}", label: "shower door" },
};

export function TodayPanel({
  world,
  plan,
  onChanged,
}: {
  world: World;
  plan: Plan | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  /** Out sick, van won't start - recorded as the same events everything else already
   * understands. When the backend absorbed the outage on its own - rerouted with
   * every promise kept - it says so, and that sentence is worth showing: the
   * difference between "the schedule broke" and "the schedule healed" is the
   * difference between a task and a notification. */
  /** "Ken texted back yes" - the dispatcher relays the reply with one tap. First
   * claim wins server-side, so two taps in two tabs cannot double-book an evening. */
  async function claim(jobId: string, workerId: string) {
    setBusy(true);
    try {
      const result = await api.claimOvertime(jobId, workerId);
      setNote(`${result.worker} took the overtime - ${result.status}`);
      onChanged();
    } catch (exc) {
      setNote((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function record(event: Record<string, unknown>) {
    setBusy(true);
    try {
      const result = await api.recordEvent(event);
      setNote(result.note ?? "");
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  /** Mark one fitter out for one calendar day: midnight to midnight, so the shift
   * and any overtime reach are both covered, and no other day is touched. */
  function dayOut(workerId: string, date: string) {
    void record({
      kind: "worker-unavailable",
      target: workerId,
      window_start: `${date}T00:00`,
      until: `${date}T23:59`,
    });
  }

  /** Bring one day back. The restore carries the day as a window, so an outage that
   * spans several days is carved around it rather than cancelled outright. */
  function dayBack(workerId: string, date: string) {
    void record({
      kind: "worker-restored",
      target: workerId,
      window_start: `${date}T00:00`,
      window_end: `${date}T23:59`,
    });
  }

  const days = world.workers[0]?.days ?? [];
  const byDate = new Map<string, Plan["routes"]>();
  for (const route of plan?.routes ?? []) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }

  return (
    <section className="panel panel--today">
      <div className="rota__split">
      <div className="rota__left">
      <h2>Crew availability</h2>
      {note && <p className="rota__healed">{note}</p>}
      <p className="panel__hint">
        Click a day to mark someone out for that day; click it again to bring them
        back. Out for hours, not days? Type it in the call box. Anyone can stay up to
        two hours past shift on overtime.
      </p>

      <div className="rota">
        <table>
          <thead>
            <tr>
              <th>fitter</th>
              {days.map((d) => (
                <th key={d.date}>{d.day}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {world.workers.map((worker) => (
              <tr key={worker.id}>
                <td className="rota__who">
                  <strong>{worker.name}</strong>
                  <span className="rota__certs">
                    {worker.certifications.map((cert) => {
                      const badge = CERT_BADGES[cert];
                      return (
                        <span key={cert} title={badge?.label ?? cert}>
                          {badge?.icon ?? "\u2022"}
                        </span>
                      );
                    })}
                  </span>
                </td>
                {worker.days.map((d) =>
                  d.shift === "off" ? (
                    <td key={d.date} className="rota__cell rota__cell--rest">
                      off
                    </td>
                  ) : !d.actionable ? (
                    // The shift is already behind the clock. A click here could not
                    // change anything, so there is nothing to click.
                    <td
                      key={d.date}
                      className="rota__cell rota__cell--past"
                      title="already past - nothing left to block out"
                    >
                      {d.available ? d.shift : "out"}
                    </td>
                  ) : d.available ? (
                    <td key={d.date} className="rota__cell">
                      <button
                        className="rota__daybtn"
                        disabled={busy}
                        title={`mark ${worker.name} out on ${d.day} (sick, absent)`}
                        onClick={() => dayOut(worker.id, d.date)}
                      >
                        {d.shift}
                        {d.extended && <span className="rota__extended">{d.extended}</span>}
                        <span className="rota__hovermark">{"\u2715"}</span>
                      </button>
                    </td>
                  ) : (
                    <td key={d.date} className="rota__cell rota__cell--out">
                      <button
                        className="rota__daybtn rota__daybtn--out"
                        disabled={busy}
                        title={`${worker.name} is back on ${d.day} - restore`}
                        onClick={() => dayBack(worker.id, d.date)}
                      >
                        out
                        <span className="rota__hovermark">{"\u21BA"}</span>
                      </button>
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="rota__legend">
        {Object.entries(CERT_BADGES).map(([key, badge]) => (
          <span key={key}>
            {badge.icon} {badge.label}
          </span>
        ))}
      </p>

      <div className="rota__vans">
        {world.vans.map((van) => (
          <span key={van.id} className={van.available ? "" : "warn"}>
            {van.id}
            {!van.available && " out"}
            <button
              className="rota__toggle"
              disabled={busy}
              title={
                van.available
                  ? `mark ${van.id} off the road until it is restored`
                  : `${van.id} is fixed - restore`
              }
              onClick={() =>
                void record({
                  kind: van.available ? "van-unavailable" : "van-restored",
                  target: van.id,
                })
              }
            >
              {van.available ? "\u2715" : "\u21BA"}
            </button>
          </span>
        ))}
      </div>

      {world.overtime_offers.length > 0 && (
        <div className="otboard">
          <h3>Overtime on offer</h3>
          {world.overtime_offers.map((offer) => (
            <div key={offer.job_id} className={`otboard__row otboard__row--${offer.status}`}>
              <span className="otboard__what">
                {offer.customer} · {offer.day} {offer.arrival} · ~
                {offer.overtime_minutes} min past shift
              </span>
              {offer.status === "claimed" ? (
                <span className="otboard__state ok">claimed by {offer.claimed_by}</span>
              ) : offer.status === "expired" ? (
                <span className="otboard__state">
                  nobody claimed by {offer.deadline} - stays with {offer.fallback}
                </span>
              ) : (
                <span className="otboard__state">
                  first yes by {offer.deadline} ·
                  {offer.offered_to.map((name, index) => (
                    <button
                      key={name}
                      className="otboard__claim"
                      disabled={busy}
                      title={`${name} texted back yes`}
                      onClick={() => void claim(offer.job_id, offer.offered_ids[index] ?? "")}
                    >
                      {name}
                    </button>
                  ))}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      </div>

      {byDate.size > 0 && (
        <div className="workload">
          <h3>How the week loads them</h3>
          {[...byDate.keys()].sort().map((date) => (
            <div key={date} className="workload__day">
              <span className="workload__dow">
                {new Date(`${date}T12:00:00`).toLocaleDateString(undefined, {
                  weekday: "short",
                })}
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
      </div>
    </section>
  );
}
