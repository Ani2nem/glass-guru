"""Caching layer for travel estimates.

The economics that make this project affordable rest on one observation: a travel
time is not a property of two addresses and a timestamp, it is a property of two
*neighbourhoods* and a *time bucket*. Two houses in the same 150m cell have the same
drive time to within noise, and 08:14 and 08:31 are both morning peak. Collapsing
the key that way turns an unbounded space into a small one that a single service
area fills up and then reuses forever.

Two consequences worth stating:

* A disruption re-solve costs nothing. The geography did not change when a van broke
  down, so every leg is already cached.
* Cost grows with distinct geography touched, not with jobs times days. Ten times the
  jobs in the same city is the same cache.

The frozen snapshot matters just as much. A committed JSON of the legs a scenario
needs makes that scenario replay identically on any machine with no network and no
spend, which is the precondition for the eval layer. ``MissPolicy.ERROR`` is what
keeps that honest: a snapshot run that silently fell back to live lookups would be
neither reproducible nor free, and would not announce itself.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from glass_guru.domain.models import Location
from glass_guru.domain.travel import TravelLeg, TravelOracle
from glass_guru.scheduler.travel.base import TravelMatrix, bucket_for


@dataclass(frozen=True, slots=True)
class LegKey:
    """What actually determines a drive time, once noise is collapsed away."""

    origin: str
    dest: str
    day_type: str
    bucket: str

    @classmethod
    def build(cls, origin: Location, dest: Location, depart_at: datetime) -> LegKey:
        return cls(
            origin=origin.geohash7,
            dest=dest.geohash7,
            day_type="weekend" if depart_at.weekday() >= 5 else "weekday",
            bucket=bucket_for(depart_at).value,
        )

    def encode(self) -> str:
        return f"{self.origin}|{self.dest}|{self.day_type}|{self.bucket}"

    @classmethod
    def decode(cls, raw: str) -> LegKey:
        origin, dest, day_type, bucket = raw.split("|")
        return cls(origin=origin, dest=dest, day_type=day_type, bucket=bucket)


class LegStore(Protocol):
    def get(self, key: LegKey) -> TravelLeg | None: ...
    def put(self, key: LegKey, leg: TravelLeg) -> None: ...


class MemoryLegStore:
    """Process-local store. The default for a single solve."""

    def __init__(self, seed: dict[LegKey, TravelLeg] | None = None) -> None:
        self._legs: dict[LegKey, TravelLeg] = dict(seed or {})

    def get(self, key: LegKey) -> TravelLeg | None:
        return self._legs.get(key)

    def put(self, key: LegKey, leg: TravelLeg) -> None:
        self._legs[key] = leg

    def __len__(self) -> int:
        return len(self._legs)

    def snapshot(self) -> dict[LegKey, TravelLeg]:
        return dict(self._legs)


class JsonLegStore:
    """File-backed store, committed alongside the scenarios it serves.

    Writes are buffered until :meth:`flush` so a solve does not touch the disk per leg.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._legs: dict[LegKey, TravelLeg] = {}
        self._dirty = False
        if self.path.exists():
            raw: dict[str, dict[str, float]] = json.loads(self.path.read_text())
            self._legs = {
                LegKey.decode(k): TravelLeg(minutes=int(v["minutes"]), miles=float(v["miles"]))
                for k, v in raw.items()
            }

    def get(self, key: LegKey) -> TravelLeg | None:
        return self._legs.get(key)

    def put(self, key: LegKey, leg: TravelLeg) -> None:
        self._legs[key] = leg
        self._dirty = True

    def __len__(self) -> int:
        return len(self._legs)

    def flush(self) -> bool:
        """Persist if anything changed. Returns whether a write happened."""
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key.encode(): {"minutes": leg.minutes, "miles": round(leg.miles, 4)}
            for key, leg in sorted(self._legs.items(), key=lambda kv: kv[0].encode())
        }
        self.path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        self._dirty = False
        return True


class MissPolicy(StrEnum):
    """What to do when a leg is not in the cache."""

    #: Ask the wrapped provider and remember the answer. Normal operation.
    COMPUTE = "compute"
    #: Refuse. Used when replaying against a frozen snapshot, so a run cannot quietly
    #: reach the network and stop being reproducible.
    ERROR = "error"


class CacheMiss(LookupError):
    """A leg was absent from a frozen snapshot."""


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


class CachingTravelProvider:
    """Wraps a travel oracle with a snapped, bucketed cache."""

    def __init__(
        self,
        inner: TravelOracle | None,
        store: LegStore | None = None,
        *,
        on_miss: MissPolicy = MissPolicy.COMPUTE,
    ) -> None:
        if inner is None and on_miss is MissPolicy.COMPUTE:
            raise ValueError("a provider is required unless on_miss is ERROR")
        self._inner = inner
        self._store = store if store is not None else MemoryLegStore()
        self._on_miss = on_miss
        self.stats = CacheStats()

    @property
    def store(self) -> LegStore:
        return self._store

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        key = LegKey.build(origin, dest, depart_at)
        cached = self._store.get(key)
        if cached is not None:
            self.stats.hits += 1
            return cached

        self.stats.misses += 1
        if self._on_miss is MissPolicy.ERROR or self._inner is None:
            raise CacheMiss(
                f"no cached leg for {key.encode()} "
                f"({origin.address or origin.geohash7} -> {dest.address or dest.geohash7}); "
                "the frozen travel snapshot needs regenerating"
            )
        computed = self._inner.leg(origin, dest, depart_at)
        self._store.put(key, computed)
        return computed

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
            row = [
                TravelLeg(0, 0.0) if origin is dest else self.leg(origin, dest, depart_at)
                for dest in locs
            ]
            minutes.append(tuple(leg.minutes for leg in row))
            miles.append(tuple(leg.miles for leg in row))
        return TravelMatrix(keys=keys, minutes=tuple(minutes), miles=tuple(miles))
