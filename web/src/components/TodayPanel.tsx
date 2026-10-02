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

/** Local date, because toISOString shifts the day east of UTC. */
function isoDate(d: Date): string {
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, "0"),
    String(d.getDate()).padStart(2, "0"),
  ].join("-");
}

/** The end of a day `plus` days from now, as an ISO stamp the API reads in the
 * business's own timezone. 23:59, because "out today" means the whole of today. */
function endOfDay(plus: number): string {
  const d = new Date();
  d.setDate(d.getDate() + plus);
  return `${isoDate(d)}T23:59`;
}

/** Days from now to the coming Sunday - "rest of the week" as a person means it. */
function daysToSunday(): number {
  return (7 - new Date().getDay()) % 7;
}

/**
 * Marking somebody out used to mean out *indefinitely*: one click emptied their whole
 * visible week, when the fact being recorded was "Marcus is sick today". The event
 * always supported an `until`; the button just never asked. So the cross asks - four
 * answers, one tap each - and "back sooner than expected" is still the restore arrow.
 */
const OUT_FOR: { label: string; until: () => string | undefined }[] = [
  { label: "rest of today", until: () => endOfDay(0) },
  { label: "today + tomorrow", until: () => endOfDay(1) },
  { label: "rest of this week", until: () => endOfDay(daysToSunday()) },
  { label: "until further notice", until: () => undefined },
];

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
  const [asking, setAsking] = useState<string | null>(null);

  /** Out sick, van won't start - recorded as the same events everything else already
   * understands, so the plan banner and the replan flow react without knowing a
   * button exists. A quiet icon, because the loud version made the panel unreadable. */
  async function record(kind: string, target: string, until?: string) {
    setBusy(true);
    setAsking(null);
    try {
      await api.recordEvent(until ? { kind, target, until } : { kind, target });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  /** The ✕ / ↺ pair plus the "for how long?" menu, shared by fitters and vans. */
  function OutControl({ id, name, kind }: { id: string; name: string; kind: string }) {
    const available = kind === "worker"
      ? world.workers.find((w) => w.id === id)?.available ?? true
      : world.vans.find((v) => v.id === id)?.available ?? true;
    if (!available) {
      return (
        <button
          className="rota__toggle"
          disabled={busy}
          title={`${name} is back - restore`}
          onClick={() => void record(`${kind}-restored`, id)}
        >
          ↺
        </button>
      );
    }
    return (
      <span className="rota__out">
        <button
          className="rota__toggle"
          disabled={busy}
          title={`mark ${name} out (sick, absent, off the road)`}
          onClick={() => setAsking(asking === id ? null : id)}
        >
          ✕
        </button>
        {asking === id && (
          <span className="rota__menu">
            <span className="rota__menu-title">out for</span>
            {OUT_FOR.map((choice) => (
              <button
                key={choice.label}
                disabled={busy}
                onClick={() => void record(`${kind}-unavailable`, id, choice.until())}
              >
                {choice.label}
              </button>
            ))}
          </span>
        )}
      </span>
    );
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
          Every route starts and ends at the shop: {world.depot_address}. Anyone can be
          kept up to two hours past shift on overtime.
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
                  <OutControl id={worker.id} name={worker.name} kind="worker" />
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
                      d.shift
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
            <OutControl id={van.id} name={van.id} kind="van" />
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
