"""Geocode the sample business's addresses for real, and report how wrong the
hand-written coordinates were.

The fixture's lat/lon were written from memory, never verified. That is fine for
arithmetic - the solver is internally consistent either way - but it becomes a
problem the moment OSRM computes real road distances between them, because a point
that lands in Puget Sound has no road network around it.

Results are cached to ``config/geocode_cache.json`` and committed, so the fixture
stays reproducible and nobody needs network access to run the tests. Re-running is
idempotent; pass --refresh to re-query.

Nominatim's usage policy requires a descriptive User-Agent and at most one request
per second. Both are respected below.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "config" / "geocode_cache.json"
ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "glass-guru-dev/0.1 (field-service scheduling fixture)"
RATE_LIMIT_SECONDS = 1.1


def geocode(query: str) -> dict[str, object] | None:
    url = f"{ENDPOINT}?{urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1})}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not payload:
        return None
    hit = payload[0]
    return {
        "lat": float(hit["lat"]),
        "lon": float(hit["lon"]),
        "display_name": hit["display_name"],
        "importance": hit.get("importance"),
    }


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 3958.7613 * math.asin(math.sqrt(h))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="re-query cached entries")
    args = parser.parse_args()

    from glass_guru.fixtures.sample_business import DEPOT, JOBS, WORKERS

    targets: list[tuple[str, str, tuple[float, float]]] = [
        ("depot", DEPOT.address, (DEPOT.lat, DEPOT.lon)),
    ]
    targets += [(job.id, job.location.address, (job.location.lat, job.location.lon)) for job in JOBS]
    targets += [
        (worker.id, worker.home_location.address,
         (worker.home_location.lat, worker.home_location.lon))
        for worker in WORKERS
    ]

    cache: dict[str, dict[str, object]] = {}
    if CACHE.exists() and not args.refresh:
        cache = json.loads(CACHE.read_text())

    print(f"{'key':<12} {'drift':>8}  resolved")
    print("-" * 96)
    drifts: list[tuple[str, float]] = []

    for key, address, invented in targets:
        # The fixture's addresses already name their city and state. An earlier
        # version appended ", Seattle, WA" to anything that did not say ", WA",
        # which quietly turned every Texas address into "…, Fort Worth, TX,
        # Seattle, WA" and matched nothing at all. Fully qualified addresses need
        # no help; the service-area bound in glass_guru.geocoding is what stops a
        # bare street name resolving to the wrong state.
        query = address
        if key not in cache:
            try:
                hit = geocode(query)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"{key:<12} {'ERROR':>8}  {exc}")
                continue
            time.sleep(RATE_LIMIT_SECONDS)
            if hit is None:
                print(f"{key:<12} {'NO HIT':>8}  {query!r}")
                continue
            hit["query"] = query
            cache[key] = hit

        entry = cache[key]
        actual = (float(entry["lat"]), float(entry["lon"]))
        drift = haversine_miles(invented, actual)
        drifts.append((key, drift))
        name = str(entry["display_name"])
        print(f"{key:<12} {drift:>7.2f}mi  {name[:70]}")

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")

    if drifts:
        worst = max(drifts, key=lambda d: d[1])
        median = sorted(d for _, d in drifts)[len(drifts) // 2]
        print("-" * 96)
        print(f"median drift {median:.2f}mi   worst {worst[0]} {worst[1]:.2f}mi")
        print(f"wrote {CACHE.relative_to(REPO)} ({len(cache)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
