import { useState } from "react";

import { api } from "../api";
import type { Plan, World } from "../types";

/**
 * The crew, as a rota rather than a list.
 *
 * The old panel said "Dan 8:00 AM - 5:00 PM" and nothing else, and the dispatcher had
 * no way to see why every after-four job landed on Dan. The answer was always in the
 * data - who holds which certification, and how late each person may legally be kept -
 * it was just never on the screen. Days across, one row per fitter, the overtime
 * reach in grey, and a symbol per skill: "why did the machine choose Dan" becomes a
 * thing you can see, not a thing you have to ask.
 */

/** One symbol per certification, readable at a squint. The legend underneath spells
 * them out; the tooltip on each chip repeats it on hover. */
const CERT_BADGES: Record<string, { icon: string; label: string }> = {
  residential_glazing: { icon: "🏠", label: "residential glazing" },
  commercial_storefront: { icon: "🏢", label: "commercial storefront" },
  auto_glass: { icon: "🚗", label: "auto glass" },
  tempered_safety: { icon: "🛡️", label: "tempered safety" },
  screen_repair: { icon: "🪟", label: "screen repair" },
  shower_door: { icon: "🚿", label: "shower door" },
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

  /** Out sick, van won't start - recorded as the same events everything else already
   * understands, so the plan banner and the replan flow react without knowing a
   * button exists. A quiet icon, because the loud version made the panel unreadable. */
  async function toggle(kind: string, target: string) {
    setBusy(true);
    try {
      await api.recordEvent({ kind, target });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  const days = world.workers[0]?.days ?? [];
  const byDate = new Map<string, Plan["routes"]>();
  for (const route of plan?.routes ?? []) {
    byDate.set(route.date, [...(byDate.get(route.date) ?? []), route]);
  }

  return (
    <section className="panel panel--today">
      <h2>Crew availability</h2>
      {world.depot_address && (
        <p className="panel__hint">
          Every route starts and ends at the shop: {world.depot_address}
        </p>
      )}

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
              <tr key={worker.id} className={worker.available ? "" : "rota__outrow"}>
                <td className="rota__who">
                  <strong>{worker.name}</strong>
                  <span className="rota__certs">
                    {worker.certifications.map((cert) => {
                      const badge = CERT_BADGES[cert];
                      return (
                        <span key={cert} title={badge?.label ?? cert}>
                          {badge?.icon ?? "•"}
                        </span>
                      );
                    })}
                  </span>
                  <button
                    className="rota__toggle"
                    disabled={busy}
                    title={
                      worker.available
                        ? `mark ${worker.name} out (sick, absent) from now`
                        : `${worker.name} is back - restore`
                    }
                    onClick={() =>
                      void toggle(
                        worker.available ? "worker-unavailable" : "worker-restored",
                        worker.id,
                      )
                    }
                  >
                    {worker.available ? "✕" : "↺"}
                  </button>
                </td>
                {worker.days.map((d) => (
                  <td
                    key={d.date}
                    className={
                      d.shift === "off" || !d.available
                        ? "rota__cell rota__cell--off"
                        : "rota__cell"
                    }
                  >
                    {!d.available ? (
                      "out"
                    ) : d.shift === "off" ? (
                      "off"
                    ) : (
                      <>
                        {d.shift}
                        {d.reach && <span className="rota__reach">{d.reach}</span>}
                      </>
                    )}
                  </td>
                ))}
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
                van.available ? `mark ${van.id} off the road` : `${van.id} is fixed - restore`
              }
              onClick={() =>
                void toggle(van.available ? "van-unavailable" : "van-restored", van.id)
              }
            >
              {van.available ? "✕" : "↺"}
            </button>
          </span>
        ))}
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
    </section>
  );
}
