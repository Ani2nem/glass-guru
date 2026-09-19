"""Travel value types owned by the domain.

The invariant checker must independently recompute travel to verify a plan, so the
*type* of a travel estimate belongs to the domain. Concrete providers (synthetic,
OSRM, OSRM + sampled traffic) live in :mod:`glass_guru.scheduler.travel` and satisfy
:class:`TravelOracle` structurally - the domain never imports the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from glass_guru.domain.models import Location


@dataclass(frozen=True, slots=True)
class TravelLeg:
    """One origin-to-destination hop."""

    minutes: int
    miles: float

    def scaled(self, multiplier: float) -> TravelLeg:
        """Apply a traffic multiplier to time only - congestion costs minutes, not miles."""
        return TravelLeg(minutes=max(0, round(self.minutes * multiplier)), miles=self.miles)


@runtime_checkable
class TravelOracle(Protocol):
    """Anything that can answer "how long from here to there, leaving then?"."""

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg: ...
