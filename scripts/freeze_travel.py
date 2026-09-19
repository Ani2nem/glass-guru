"""Freeze every travel leg the fixture needs into a committed snapshot.

Why this exists: tests and golden scenarios should run on *real* road distances, but
must not require a running OSRM, a network connection, or any spend. Querying OSRM
once and committing the answers gives all three - scenarios replay byte-identically
on any machine, and a missing leg fails loudly rather than silently falling back to
a straight line.

The snapshot is keyed the same way the live cache is: origin and destination geohash-7
cells, weekday-or-weekend, and time bucket. That is the resolution at which a drive
time is actually determined, so the file stays small even as the fixture grows.

Run after changing addresses, the map extract, or the traffic profile:

    docker compose up -d osrm
    python scripts/freeze_travel.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "config" / "travel_snapshot.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osrm-url", default="http://localhost:5000")
    parser.add_argument("--out", type=Path, default=SNAPSHOT)
    args = parser.parse_args()

    from glass_guru.config import BusinessParams
    from glass_guru.fixtures.sample_business import DEPOT, JOBS, WORKERS, BUSINESS_TZ, WEEK_START
    from glass_guru.scheduler.travel.cache import CachingTravelProvider, JsonLegStore
    from glass_guru.scheduler.travel.osrm import OsrmTravelProvider, OsrmUnavailable

    business = BusinessParams.load()
    osrm = OsrmTravelProvider.from_business(business, args.osrm_url)
    if not osrm.health():
        print(
            f"OSRM is not reachable at {args.osrm_url}.\n"
            "Start it with:  docker compose up -d osrm\n"
            "First time?     ./scripts/setup_osrm.sh",
            file=sys.stderr,
        )
        return 2

    store = JsonLegStore(args.out)
    provider = CachingTravelProvider(osrm, store)

    # Depot, every job site, and every worker's home: the complete set of points a
    # route can start from, pass through, or return to.
    places = [DEPOT, *(job.location for job in JOBS), *(w.home_location for w in WORKERS)]

    # One probe per bucket per day type. The key collapses times within a bucket, so
    # these few samples cover every departure time a solve can ask about.
    probes: list[datetime] = []
    for day_offset in (0, 5):  # a weekday and a Saturday
        day = datetime.combine(
            WEEK_START + timedelta(days=day_offset),
            datetime.min.time(),
            tzinfo=BUSINESS_TZ,
        )
        probes += [day + timedelta(hours=h) for h in (6, 8, 12, 16, 19)]

    total = 0
    failures = 0
    for origin in places:
        for dest in places:
            if origin.geohash7 == dest.geohash7:
                continue
            for probe in probes:
                try:
                    provider.leg(origin, dest, probe)
                    total += 1
                except OsrmUnavailable as exc:
                    failures += 1
                    print(f"  ! {origin.address} -> {dest.address}: {exc}", file=sys.stderr)

    wrote = store.flush()
    print(
        f"{len(store)} legs cached from {len(places)} places x {len(probes)} probe times"
        f"  ({provider.stats.hits} hits, {provider.stats.misses} lookups)"
    )
    if failures:
        print(f"{failures} leg(s) failed - check those coordinates are inside the extract")
    print(f"{'wrote' if wrote else 'unchanged'} {args.out.relative_to(REPO)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
