"""Travel times from a self-hosted OSRM instance.

OSRM gives the half of the problem that is pure geometry: a real road network with
turn restrictions, one-ways and bridges, computed locally for free and pinned to a
map extract so results are reproducible. It knows nothing about congestion, which is
exactly the split the cost model assumes::

    travel_time(A -> B, t) = free_flow_time(A -> B) x traffic_multiplier(bucket)

Free-flow comes from here at zero marginal cost. The multiplier is small, coarse and
low-dimensional - a handful of numbers per corridor per time bucket - which is why it
can be sampled from a paid API weekly rather than queried per leg per solve. Paying a
maps provider for the geometry, which never changes, is what makes naive designs
expensive.

Run the backend with ``docker compose up osrm`` (see ``docker/osrm/``). Distances come
back in metres and durations in seconds; both are converted at the boundary so nothing
downstream has to remember which unit OSRM speaks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import httpx

from glass_guru.config import BusinessParams
from glass_guru.domain.models import Location
from glass_guru.domain.travel import TravelLeg
from glass_guru.scheduler.travel.base import TimeBucket, TravelMatrix, bucket_for
from glass_guru.scheduler.travel.synthetic import TRAFFIC_PROFILE

METERS_PER_MILE = 1609.344
DEFAULT_BASE_URL = "http://localhost:5000"


class OsrmUnavailable(RuntimeError):
    """The OSRM backend could not be reached or refused the request."""


class OsrmTravelProvider:
    """Free-flow road-network travel, with the time-of-day profile layered on top."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        traffic_profile: dict[TimeBucket, float] | None = None,
        approach_minutes: int = 2,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._profile = dict(traffic_profile or TRAFFIC_PROFILE)
        self._approach_minutes = approach_minutes
        self._client = client or httpx.Client(timeout=timeout)

    @classmethod
    def from_business(
        cls, business: BusinessParams, base_url: str = DEFAULT_BASE_URL
    ) -> OsrmTravelProvider:
        return cls(
            base_url,
            traffic_profile={
                TimeBucket(name): param.value
                for name, param in business.travel.traffic_multipliers.items()
            },
            approach_minutes=int(business.travel.approach_minutes.value),
        )

    # ------------------------------------------------------------------ transport

    def health(self) -> bool:
        """Whether the backend is up and answering routing queries."""
        try:
            self._table([Location(lat=47.60, lon=-122.33), Location(lat=47.62, lon=-122.35)])
        except Exception:
            return False
        return True

    def _table(self, locations: Sequence[Location]) -> tuple[list[list[float]], list[list[float]]]:
        """Raw OSRM table: durations in seconds, distances in metres."""
        coords = ";".join(f"{loc.lon:.6f},{loc.lat:.6f}" for loc in locations)
        url = f"{self._base_url}/table/v1/driving/{coords}"
        try:
            response = self._client.get(url, params={"annotations": "duration,distance"})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise OsrmUnavailable(f"OSRM request failed: {exc}") from exc

        if payload.get("code") != "Ok":
            raise OsrmUnavailable(f"OSRM returned {payload.get('code')}: {payload.get('message')}")

        durations = payload.get("durations")
        distances = payload.get("distances")
        if durations is None or distances is None:
            raise OsrmUnavailable("OSRM response omitted durations or distances")
        return durations, distances

    # ---------------------------------------------------------------- conversion

    def _to_leg(self, seconds: float | None, meters: float | None, at: datetime) -> TravelLeg:
        """Apply congestion to time only - a jam costs minutes, never miles.

        A ``None`` entry means OSRM could not snap a coordinate to the network, which
        usually means the point is outside the map extract. Surfacing that as an
        exception is deliberate: silently substituting a straight line would let a
        badly geocoded address quietly poison every route it touches.
        """
        if seconds is None or meters is None:
            raise OsrmUnavailable(
                "OSRM could not route between these points; a coordinate is probably "
                "outside the loaded map extract"
            )
        multiplier = self._profile[bucket_for(at)]
        minutes = round(seconds / 60.0 * multiplier) + self._approach_minutes
        return TravelLeg(minutes=max(1, minutes), miles=round(meters / METERS_PER_MILE, 3))

    # -------------------------------------------------------------------- oracle

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        if origin.lat == dest.lat and origin.lon == dest.lon:
            return TravelLeg(minutes=0, miles=0.0)
        durations, distances = self._table([origin, dest])
        return self._to_leg(durations[0][1], distances[0][1], depart_at)

    def matrix(
        self,
        keyed_locations: Sequence[tuple[str, Location]],
        depart_at: datetime,
    ) -> TravelMatrix:
        """One request for the whole matrix - OSRM's table endpoint is far cheaper
        than N^2 route calls, and the backend is configured with a raised
        ``--max-table-size`` to allow it."""
        keys = tuple(k for k, _ in keyed_locations)
        locs = [loc for _, loc in keyed_locations]
        durations, distances = self._table(locs)

        minutes: list[tuple[int, ...]] = []
        miles: list[tuple[float, ...]] = []
        for i in range(len(locs)):
            row = [
                TravelLeg(0, 0.0)
                if i == j
                else self._to_leg(durations[i][j], distances[i][j], depart_at)
                for j in range(len(locs))
            ]
            minutes.append(tuple(leg.minutes for leg in row))
            miles.append(tuple(leg.miles for leg in row))
        return TravelMatrix(keys=keys, minutes=tuple(minutes), miles=tuple(miles))

    def close(self) -> None:
        self._client.close()
