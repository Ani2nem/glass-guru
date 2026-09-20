"""Choosing a travel provider.

Four modes, because the right answer differs by context and getting it wrong is
expensive in different ways each time:

``frozen``
    Real road distances from a committed snapshot. No network, no spend, byte-identical
    on every machine. This is the default, and what tests and golden scenarios use: a
    missing leg raises rather than silently falling back, so a run cannot quietly stop
    being reproducible.
``osrm``
    Live queries against a self-hosted backend. What you want while changing addresses
    or the map extract, and what regenerates the snapshot.
``synthetic``
    Haversine times a constant. No dependencies at all. Useful for unit tests of logic
    that has nothing to do with geography, and as a fallback before OSRM is set up -
    but it does not know about bridges, so it is not a substitute for real roads.
``warm``
    The frozen snapshot as a warm start, with live OSRM for anything missing. This is
    the production shape: a quote for a brand-new address needs a handful of new legs,
    and everything else is already paid for. Known legs cost nothing, misses cost one
    lookup each.
``auto``
    Frozen when the snapshot exists, synthetic otherwise. Keeps a fresh clone working
    before anyone runs ``scripts/setup_osrm.sh``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from glass_guru.config import BusinessParams
from glass_guru.scheduler.travel.base import TravelProvider
from glass_guru.scheduler.travel.cache import (
    CachingTravelProvider,
    JsonLegStore,
    MissPolicy,
)
from glass_guru.scheduler.travel.osrm import DEFAULT_BASE_URL, OsrmTravelProvider
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[4] / "config" / "travel_snapshot.json"


class TravelMode(StrEnum):
    AUTO = "auto"
    FROZEN = "frozen"
    OSRM = "osrm"
    SYNTHETIC = "synthetic"
    WARM = "warm"


def build_travel(
    business: BusinessParams,
    mode: TravelMode | str = TravelMode.AUTO,
    *,
    snapshot: Path | None = None,
    osrm_url: str = DEFAULT_BASE_URL,
) -> TravelProvider:
    """Build the travel oracle for a given mode."""
    mode = TravelMode(mode)
    path = snapshot or DEFAULT_SNAPSHOT

    if mode is TravelMode.AUTO:
        mode = TravelMode.FROZEN if path.exists() else TravelMode.SYNTHETIC

    if mode is TravelMode.SYNTHETIC:
        return SyntheticTravelProvider.from_business(business)

    if mode is TravelMode.OSRM:
        return OsrmTravelProvider.from_business(business, osrm_url)

    if mode is TravelMode.WARM:
        store = JsonLegStore(path) if path.exists() else None
        return CachingTravelProvider(
            OsrmTravelProvider.from_business(business, osrm_url),
            store,
            on_miss=MissPolicy.COMPUTE,
        )

    if not path.exists():
        raise FileNotFoundError(
            f"no travel snapshot at {path}. Generate one with:\n"
            "  docker compose up -d osrm\n"
            "  python scripts/freeze_travel.py"
        )
    # Refuse rather than compute on a miss: a frozen run that quietly reached the
    # network would be neither reproducible nor free, and would not say so.
    return CachingTravelProvider(None, JsonLegStore(path), on_miss=MissPolicy.ERROR)


def describe(mode: TravelMode | str, snapshot: Path | None = None) -> str:
    """One line naming the travel source, for the top of a rendered board."""
    resolved = TravelMode(mode)
    path = snapshot or DEFAULT_SNAPSHOT
    if resolved is TravelMode.AUTO:
        resolved = TravelMode.FROZEN if path.exists() else TravelMode.SYNTHETIC
    return {
        TravelMode.FROZEN: f"frozen OSRM snapshot ({path.name})",
        TravelMode.OSRM: "live OSRM",
        TravelMode.SYNTHETIC: "synthetic (straight line x road factor - no real roads)",
        TravelMode.WARM: f"frozen snapshot ({path.name}) warm-starting live OSRM",
    }[resolved]
