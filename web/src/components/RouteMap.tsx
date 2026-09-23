import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip } from "react-leaflet";
import type { Plan } from "../types";

// One colour per crew, reused across days. A dispatcher tracks "the blue van" rather
// than a crew id, so consistency matters more than the particular palette.
const COLOURS = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];

export function RouteMap({ plan, day }: { plan: Plan; day: string | null }) {
  const routes = plan.routes.filter((r) => !day || r.date === day);
  const depot: [number, number] = plan.depot.length === 2
    ? [plan.depot[0]!, plan.depot[1]!]
    // The depot, for a plan with no stops to centre on. Haslet, Texas.
    : [33.0020, -97.3424];

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
        // Out from the depot, through each stop, and home again - the shape of the
        // day, which is what makes a bad sequence obvious at a glance.
        const path: [number, number][] = [
          depot,
          ...route.stops.map((s) => [s.lat, s.lon] as [number, number]),
          depot,
        ];
        return (
          <div key={`${route.date}-${route.crew_id}`}>
            <Polyline positions={path} pathOptions={{ color: colour, weight: 3, opacity: 0.75 }} />
            {route.stops.map((stop, order) => (
              <CircleMarker
                key={stop.job_id}
                center={[stop.lat, stop.lon]}
                radius={9}
                pathOptions={{ color: colour, fillColor: colour, fillOpacity: 0.9 }}
              >
                <Tooltip>
                  <strong>{order + 1}. {stop.customer_name}</strong>
                  <br />
                  {route.worker_names.join(" + ")} · {stop.arrival.slice(11, 16)}
                </Tooltip>
              </CircleMarker>
            ))}
          </div>
        );
      })}
    </MapContainer>
  );
}
