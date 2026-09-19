"""Deterministic synthetic travel provider.

Pure function of its inputs - no network, no clock, no randomness - so unit tests and
golden scenarios replay identically and cost nothing. Routes are geometrically naive
(it cannot know about bridges or one-ways), so it is the test default. Real road
networks arrive with the OSRM provider in a later increment; until then every
distance in this system is a straight line times a constant.

Speed rises with trip length, which is the single most important non-linearity to
capture: a one-mile urban hop averages ~24 mph while a 25-mile run uses highway and
averages ~48. Modelling travel at one flat speed makes short trips look cheap and long
trips look free, and the solver will happily build a route out of the resulting fiction.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from glass_guru.config import BusinessParams
from glass_guru.domain.models import Location
from glass_guru.domain.travel import TravelLeg
from glass_guru.scheduler.travel.base import (
    TimeBucket,
    TravelMatrix,
    bucket_for,
)

#: Straight-line to road distance. 1.3 is the usual planar-grid figure.
ROAD_FACTOR = 1.3

#: Fixed per-leg overhead: parking, approach, walking to the door.
APPROACH_MINUTES = 2

#: Congestion by time bucket, as a multiplier on free-flow minutes.
TRAFFIC_PROFILE: dict[TimeBucket, float] = {
    TimeBucket.EARLY: 0.95,
    TimeBucket.AM_PEAK: 1.45,
    TimeBucket.MIDDAY: 1.05,
    TimeBucket.PM_PEAK: 1.50,
    TimeBucket.EVENING: 0.90,
}


def _free_flow_mph(miles: float) -> float:
    """Average speed for a trip of this length, capped at highway speed."""
    return min(55.0, 18.0 + 6.0 * math.sqrt(max(miles, 0.0)))


class SyntheticTravelProvider:
    """Haversine-based travel with a time-of-day profile."""

    def __init__(
        self,
        road_factor: float = ROAD_FACTOR,
        approach_minutes: int = APPROACH_MINUTES,
        traffic_profile: dict[TimeBucket, float] | None = None,
    ) -> None:
        self._road_factor = road_factor
        self._approach_minutes = approach_minutes
        self._profile = dict(traffic_profile or TRAFFIC_PROFILE)

    @classmethod
    def from_business(cls, business: BusinessParams) -> SyntheticTravelProvider:
        """Build from ``config/business_params.yaml`` so the road factor and
        traffic profile are auditable alongside every other business number."""
        return cls(
            road_factor=business.travel.road_factor.value,
            approach_minutes=int(business.travel.approach_minutes.value),
            traffic_profile={
                TimeBucket(name): param.value
                for name, param in business.travel.traffic_multipliers.items()
            },
        )

    def _miles(self, origin: Location, dest: Location) -> float:
        return origin.haversine_miles(dest) * self._road_factor

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        miles = self._miles(origin, dest)
        if miles <= 0.0:
            return TravelLeg(minutes=0, miles=0.0)
        free_flow = miles / _free_flow_mph(miles) * 60.0
        multiplier = self._profile[bucket_for(depart_at)]
        minutes = round(free_flow * multiplier) + self._approach_minutes
        return TravelLeg(minutes=max(1, minutes), miles=round(miles, 3))

    def matrix(
        self,
        keyed_locations: Sequence[tuple[str, Location]],
        depart_at: datetime,
    ) -> TravelMatrix:
        keys = tuple(k for k, _ in keyed_locations)
        locs = [loc for _, loc in keyed_locations]
        minutes: list[tuple[int, ...]] = []
        miles: list[tuple[float, ...]] = []
        for origin in locs:
            legs = [self.leg(origin, dest, depart_at) for dest in locs]
            minutes.append(tuple(leg.minutes for leg in legs))
            miles.append(tuple(leg.miles for leg in legs))
        return TravelMatrix(keys=keys, minutes=tuple(minutes), miles=tuple(miles))
