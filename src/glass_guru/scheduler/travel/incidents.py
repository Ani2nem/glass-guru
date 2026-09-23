"""Live traffic incidents, turned into something the solver already understands.

Two different things get called "traffic", and conflating them is how a change like
this goes wrong.

**Recurring congestion** is that the northbound run to Denton is slow every weekday at
half past four. It is a property of the road and the clock, it is knowable in advance,
and it is what the multipliers in ``business_params.yaml`` model. Those multipliers are
invented numbers and replacing them with measured ones is the single most valuable
calibration left in this system - but the measurement they need is *typical speed by
corridor by hour*, sampled once and cached, not a live feed.

**Incidents** are that a lorry has shed a load on I-35W right now. They are events:
they start, they end, and they are unknowable until they happen. They are already a
first-class thing here - ``TrafficDelay`` in the event log, ``TrafficOverride`` in
world state, ``OverrideAdjustedProvider`` layering it over any travel provider - and
until now the only way one arrived was a dispatcher typing it.

This module gives that path a second source. It does not touch the multipliers, and
replacing them with incident data would be a downgrade: a feed reporting nothing on a
clear Tuesday afternoon would leave the planner believing the evening peak does not
exist.

Nothing here mutates a matrix directly. An incident becomes a ``TrafficOverride`` and
takes the route every other disruption takes, which means the invariant checker, the
plan diff and the event log all understand it for free.

Off unless configured. Live data and reproducible evals are incompatible, so the
default feed returns nothing and the frozen travel snapshot stays exact.
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from glass_guru.domain.models import Location
from glass_guru.domain.state import TrafficOverride
from glass_guru.obs.tracing import span

#: How long an incident is assumed to last when the feed does not say. Long enough to
#: matter to the day being planned, short enough that a cleared incident stops
#: affecting the plan within one re-solve.
DEFAULT_DURATION = timedelta(hours=2)

#: TomTom reports severity 0-4 rather than a ratio. A multiplier needs one, so these
#: are a reading of what each band means for a corridor that contains the incident,
#: not a measurement. They are deliberately modest: an incident slows part of a trip,
#: and treating the whole corridor as stopped would move more work than the incident
#: justifies.
SEVERITY_MULTIPLIER: dict[int, float] = {
    0: 1.00,  # unknown
    1: 1.10,  # minor
    2: 1.25,  # moderate
    3: 1.50,  # major
    4: 2.00,  # undefined/severe, road typically closed
}


@dataclass(frozen=True, slots=True)
class Incident:
    """One reported disruption, in terms this system can use."""

    where: Location
    severity: int
    description: str
    started_at: datetime
    ends_at: datetime | None = None

    @property
    def multiplier(self) -> float:
        return SEVERITY_MULTIPLIER.get(self.severity, 1.0)


class IncidentFeed(Protocol):
    def fetch(
        self, centre: Location, radius_miles: float, now: datetime
    ) -> tuple[Incident, ...]: ...


class NoIncidentFeed:
    """The default. Says nothing, so a solve stays reproducible."""

    def fetch(self, centre: Location, radius_miles: float, now: datetime) -> tuple[Incident, ...]:
        return ()


class ScriptedIncidentFeed:
    """A fixed list, for tests and for replaying a real afternoon offline."""

    def __init__(self, incidents: tuple[Incident, ...]) -> None:
        self._incidents = incidents

    def fetch(self, centre: Location, radius_miles: float, now: datetime) -> tuple[Incident, ...]:
        return self._incidents


class TomTomIncidentFeed:
    """TomTom's Traffic Incident Details API.

    Chosen over the alternatives for one reason: its free tier is measured in requests
    per day rather than per month, and this needs one request per solve over a single
    metropolitan bounding box - a few dozen a day, against a documented 2,500.

    NOT verified against the live service. The request shape and the fields read below
    come from the published API description; no key was available to run it. Anything
    this returns should be treated as unproven until somebody points it at the real
    endpoint, which is why the default feed is the one that returns nothing.
    """

    ENDPOINT = "https://api.tomtom.com/traffic/services/5/incidentDetails"

    def __init__(self, api_key: str, timeout: float = 8.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def fetch(self, centre: Location, radius_miles: float, now: datetime) -> tuple[Incident, ...]:
        # A degree of latitude is about 69 miles; longitude shrinks with the cosine of
        # latitude. Same arithmetic as the geocoder's bounding box, and wrong in the
        # same way if it is treated as square.
        lat_pad = radius_miles / 69.0
        lon_pad = radius_miles / (69.0 * max(0.1, math.cos(math.radians(centre.lat))))
        bbox = (
            f"{centre.lon - lon_pad:.4f},{centre.lat - lat_pad:.4f},"
            f"{centre.lon + lon_pad:.4f},{centre.lat + lat_pad:.4f}"
        )
        query = urllib.parse.urlencode(
            {
                "key": self.api_key,
                "bbox": bbox,
                "fields": "{incidents{type,geometry{type,coordinates},"
                "properties{iconCategory,magnitudeOfDelay,events{description},"
                "startTime,endTime,delay}}}",
                "language": "en-GB",
            }
        )
        with span("traffic.tomtom", radius_miles=radius_miles):
            request = urllib.request.Request(
                f"{self.ENDPOINT}?{query}", headers={"User-Agent": "glass-guru/0.1"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)

        return tuple(self._parse(item, now) for item in payload.get("incidents", []) if item)

    @staticmethod
    def _parse(item: dict[str, object], now: datetime) -> Incident:
        properties = item.get("properties") or {}
        assert isinstance(properties, dict)
        geometry = item.get("geometry") or {}
        assert isinstance(geometry, dict)

        # A Point's coordinates *are* the pair; a LineString's are a list of pairs and
        # a Polygon's a list of those. Descend until numbers, rather than assuming a
        # shape - the first version took element zero of a Point and got its longitude,
        # then asked that float for its length.
        point = geometry.get("coordinates") or []
        while isinstance(point, list) and point and isinstance(point[0], list):
            point = point[0]
        lon, lat = (
            (float(point[0]), float(point[1]))
            if isinstance(point, list) and len(point) >= 2
            else (0.0, 0.0)
        )

        events = properties.get("events") or []
        description = ""
        if isinstance(events, list) and events and isinstance(events[0], dict):
            description = str(events[0].get("description", ""))

        return Incident(
            where=Location(lat=lat, lon=lon, address=description),
            severity=int(str(properties.get("magnitudeOfDelay") or 0)),
            description=description,
            started_at=now,
            ends_at=None,
        )


def overrides_from(
    incidents: tuple[Incident, ...], now: datetime, default_duration: timedelta = DEFAULT_DURATION
) -> tuple[TrafficOverride, ...]:
    """Turn incidents into the corridor multipliers the travel layer already applies.

    Scoped to the geohash-5 cell the incident sits in, and to corridors *arriving*
    there as well as leaving it - a blockage slows both directions of the traffic that
    has to pass through the cell.

    Deliberately coarse. Knowing which matrix cells a given road segment actually
    appears in would mean asking OSRM for the geometry of every pair rather than one
    table, which is 272 route calls instead of 1 for this fixture and grows with the
    square of the stops. A cell-level override is the resolution the travel cache
    already works at, and matching it keeps one notion of "corridor" in the system
    rather than two.
    """
    out: list[TrafficOverride] = []
    for incident in incidents:
        if incident.multiplier <= 1.0:
            continue
        cell = incident.where.geohash5
        ends = incident.ends_at or (now + default_duration)
        out.append(
            TrafficOverride(
                origin_geohash5=cell,
                dest_geohash5=None,
                multiplier=incident.multiplier,
                from_time=incident.started_at,
                until_time=ends,
            )
        )
        out.append(
            TrafficOverride(
                origin_geohash5=None,
                dest_geohash5=cell,
                multiplier=incident.multiplier,
                from_time=incident.started_at,
                until_time=ends,
            )
        )
    return tuple(out)


def build_feed(name: str | None = None) -> IncidentFeed:
    """``none`` or ``tomtom``, from ``GLASS_GURU_TRAFFIC_FEED``.

    Defaults to none. A live feed makes two solves of the same problem differ, which
    is exactly what the frozen travel snapshot exists to prevent, so switching it on
    is a deployment decision rather than a default.
    """
    resolved = (name or os.environ.get("GLASS_GURU_TRAFFIC_FEED", "none")).lower()
    if resolved in {"", "none", "off"}:
        return NoIncidentFeed()
    if resolved == "tomtom":
        key = os.environ.get("TOMTOM_API_KEY", "")
        if not key:
            raise ValueError(
                "GLASS_GURU_TRAFFIC_FEED=tomtom needs TOMTOM_API_KEY. "
                "Unset both to plan without live incidents."
            )
        return TomTomIncidentFeed(key)
    raise ValueError(f"unknown traffic feed {resolved!r}; expected 'none' or 'tomtom'")
