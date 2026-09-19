"""Travel-time abstraction.

The cost model that keeps this affordable::

    travel_time(A -> B, t) = free_flow_time(A -> B) x traffic_multiplier(corridor, t)

``free_flow_time`` is road-network geometry and essentially never changes, so it comes
from a free, deterministic source (synthetic in tests, self-hosted OSRM in dev and
production). ``traffic_multiplier`` is the only thing a paid maps API knows that OSRM
does not, and it is low-dimensional - a corridor and a time bucket, not a leg - which
is why calibration costs a few hundred elements a week instead of tens of thousands
per solve.

Providers are pure: they know nothing about ``WorldState``. Event-driven traffic
(``TrafficDelay``) is layered on top by :class:`OverrideAdjustedProvider`, which keeps
"I-5 is backed up" a first-class domain event rather than a fudge factor buried in a
routing client.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from glass_guru.domain.models import Location
from glass_guru.domain.travel import TravelLeg


class TimeBucket(StrEnum):
    """Coarse time-of-day bands. The traffic layer is calibrated per bucket.

    Five buckets is the resolution that matters: finer buckets multiply calibration
    cost without changing a route, coarser ones lose rush hour entirely.
    """

    EARLY = "early"
    AM_PEAK = "am_peak"
    MIDDAY = "midday"
    PM_PEAK = "pm_peak"
    EVENING = "evening"


def bucket_for(moment: datetime) -> TimeBucket:
    """Classify a local-time moment into its traffic bucket.

    Weekends collapse to MIDDAY: there is no commute peak, and pretending otherwise
    would make Saturday routes pessimistic.
    """
    if moment.weekday() >= 5:
        return TimeBucket.MIDDAY
    minutes = moment.hour * 60 + moment.minute
    if minutes < 7 * 60:
        return TimeBucket.EARLY
    if minutes < 9 * 60 + 30:
        return TimeBucket.AM_PEAK
    if minutes < 15 * 60:
        return TimeBucket.MIDDAY
    if minutes < 18 * 60 + 30:
        return TimeBucket.PM_PEAK
    return TimeBucket.EVENING


@dataclass(frozen=True, slots=True)
class TravelMatrix:
    """Dense minutes/miles between a fixed, ordered set of keyed locations.

    Keys are caller-chosen stable identifiers (job ids, ``"depot"``, ``"home:w-dan"``)
    so a matrix can be logged, cached, and diffed without positional bookkeeping.
    """

    keys: tuple[str, ...]
    minutes: tuple[tuple[int, ...], ...]
    miles: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        n = len(self.keys)
        if len(self.minutes) != n or any(len(row) != n for row in self.minutes):
            raise ValueError("minutes matrix is not square against keys")
        if len(self.miles) != n or any(len(row) != n for row in self.miles):
            raise ValueError("miles matrix is not square against keys")

    def index_of(self, key: str) -> int:
        try:
            return self.keys.index(key)
        except ValueError as exc:
            raise KeyError(f"location key {key!r} not in travel matrix") from exc

    def leg(self, origin_key: str, dest_key: str) -> TravelLeg:
        i, j = self.index_of(origin_key), self.index_of(dest_key)
        return TravelLeg(minutes=self.minutes[i][j], miles=self.miles[i][j])


@runtime_checkable
class TravelProvider(Protocol):
    """Source of free-flow-plus-traffic travel estimates."""

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        """Travel from ``origin`` to ``dest`` departing at ``depart_at``."""
        ...

    def matrix(
        self,
        keyed_locations: Sequence[tuple[str, Location]],
        depart_at: datetime,
    ) -> TravelMatrix:
        """Dense matrix over ``keyed_locations``, all departing at ``depart_at``."""
        ...


class OverrideAdjustedProvider:
    """Wraps a provider and applies event-driven traffic overrides on top.

    Keeps providers pure and makes a ``TrafficDelay`` event affect routing without
    any provider knowing the event log exists.
    """

    def __init__(
        self,
        inner: TravelProvider,
        multiplier_fn: Callable[[str, str, datetime], float],
    ) -> None:
        self._inner = inner
        self._multiplier_fn = multiplier_fn

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        base = self._inner.leg(origin, dest, depart_at)
        return base.scaled(self._multiplier_fn(origin.geohash5, dest.geohash5, depart_at))

    def matrix(
        self,
        keyed_locations: Sequence[tuple[str, Location]],
        depart_at: datetime,
    ) -> TravelMatrix:
        base = self._inner.matrix(keyed_locations, depart_at)
        locs = [loc for _, loc in keyed_locations]
        adjusted = tuple(
            tuple(
                max(
                    0,
                    round(
                        base.minutes[i][j]
                        * self._multiplier_fn(locs[i].geohash5, locs[j].geohash5, depart_at)
                    ),
                )
                for j in range(len(locs))
            )
            for i in range(len(locs))
        )
        return TravelMatrix(keys=base.keys, minutes=adjusted, miles=base.miles)
