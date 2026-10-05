import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip } from "react-leaflet";
import type { Plan } from "../types";

function twelveHour(iso: string): string {
  const hour24 = Number(iso.slice(11, 13));
  const hour = hour24 % 12 || 12;
  return `${hour}:${iso.slice(14, 16)} ${hour24 < 12 ? "AM" : "PM"}`;
}

// One colour per crew, reused across days. A dispatcher tracks "the blue van" rather
// than a crew id, so consistency matters more than the particular palette.
const COLOURS = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];

export function RouteMap({ plan, day }: { plan: Plan; day: string | null }) {
  const routes = plan.routes.filter((r) => !day || r.date === day);
  const depot: [number, number] = plan.depot.length === 2
    ? [plan.depot[0]!, plan.depot[1]!]
    // The depot, for a plan with no stops to centre on: 1150 Blue Mound Rd W.
    : [32.946396, -97.379865];

  return (
    <MapContainer center={depot} zoom={11} className="map" scrollWheelZoom>
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <CircleMarker center={depot} radius={7} pathOptions={{ color: "#111", fillOpacity: 1 }}>
        <Tooltip>Depot</Tooltip>
      </CircleMarker>

      {routes.map((route, index) => {
        const colour = COLOURS[index % COLOURS.length]!;
        // Out from the depot, through each stop in order, and home again. The
        // working legs are solid and the final run home is dashed, because two
        // straight lines from near-collinear points used to read as two separate
        // from-the-shop trips - the owner asked whether the van ever chained at
        // all, about a route that was chaining the whole time.
        const stops = route.stops.map((s) => [s.lat, s.lon] as [number, number]);
        const working: [number, number][] = [depot, ...stops];
        const home: [number, number][] = stops.length
          ? [stops[stops.length - 1]!, depot]
          : [];
        return (
          <div key={`${route.date}-${route.crew_id}`}>
            <Polyline
              positions={working}
              pathOptions={{ color: colour, weight: 4, opacity: 0.85 }}
            />
            {home.length > 0 && (
              <Polyline
                positions={home}
                pathOptions={{ color: colour, weight: 2.5, opacity: 0.5, dashArray: "6 8" }}
              />
            )}
            {route.stops.map((stop, order) => (
              <CircleMarker
                key={stop.job_id}
                center={[stop.lat, stop.lon]}
                radius={11}
                pathOptions={{ color: colour, fillColor: colour, fillOpacity: 0.95 }}
              >
                {/* The visit order lives ON the pin, not behind a hover - the
                    sequence is the whole story the map exists to tell. */}
                <Tooltip
                  permanent
                  direction="center"
                  className="map__ordernum"
                  interactive={false}
                >
                  {order + 1}
                </Tooltip>
                <Tooltip direction="top" offset={[0, -10]}>
                  <strong>{order + 1}. {stop.customer_name}</strong>
                  <br />
                  {route.worker_names.join(" + ")} · {twelveHour(stop.arrival)}
                </Tooltip>
              </CircleMarker>
            ))}
          </div>
        );
      })}
    </MapContainer>
  );
}
