"""Turning an address into a point on the map.

Cache-first: the committed ``config/geocode_cache.json`` answers instantly for
anything the fixture already knows, and only a genuinely new address reaches the
network. That keeps tests offline and keeps a live call fast, since a dispatcher
typing an address mid-conversation cannot wait on a rate-limited public service.

Nominatim's usage policy requires a descriptive User-Agent and at most one request
per second. Both are respected. For production volume this would move behind a paid
geocoder, which is why the lookup sits behind one function.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from glass_guru.domain.models import Location

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "config" / "geocode_cache.json"
ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "glass-guru/0.1 (field-service scheduling)"
RATE_LIMIT_SECONDS = 1.1


class GeocodeError(RuntimeError):
    """The address could not be resolved."""


class OutsideServiceArea(GeocodeError):
    """Resolved, but nowhere this business can send a van.

    Its own class because it is not a failure to understand the address - it is a
    business answer, and the dispatcher needs to hear "that is out of our area", not
    "something went wrong".
    """

    def __init__(self, address: str, miles: float, limit: float) -> None:
        super().__init__(
            f"{address!r} is {miles:,.0f} miles from the depot, outside the "
            f"{limit:,.0f} mile service area"
        )
        self.address = address
        self.miles = miles
        self.limit = limit


def for_service_area(
    depot: Location | None = None,
    radius_miles: float | None = None,
    **kwargs: object,
) -> Geocoder:
    """A geocoder that will only return somewhere a van could actually be sent.

    One place, rather than three call sites each remembering to pass the bounds. The
    bug this prevents is not exotic: "2nd ave" during a Seattle call resolved to
    Manhattan, and the first thing that noticed was the travel cache, several layers
    down, as a 500.
    """
    from glass_guru.config import BusinessParams
    from glass_guru.fixtures.sample_business import DEPOT

    if radius_miles is None:
        radius_miles = float(BusinessParams.load().service_area.radius_miles.value)
    return Geocoder(near=depot or DEPOT, radius_miles=radius_miles, **kwargs)  # type: ignore[arg-type]


class Geocoder:
    """Address to :class:`~glass_guru.domain.models.Location`, cache first."""

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        allow_network: bool = True,
        near: Location | None = None,
        radius_miles: float | None = None,
    ) -> None:
        self.cache_path = cache_path or DEFAULT_CACHE
        self._allow_network = allow_network
        self.near = near
        self.radius_miles = radius_miles
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._by_key: dict[str, dict[str, object]] = {}
        self._by_query: dict[str, dict[str, object]] = {}
        if self.cache_path.exists():
            self._by_key = json.loads(self.cache_path.read_text())
            self._by_query = {
                str(entry.get("query", "")).lower(): entry
                for entry in self._by_key.values()
                if entry.get("query")
            }

    def by_key(self, key: str) -> Location | None:
        """Look up a pre-resolved fixture entity (``depot``, ``j-401``, ``w-dan``)."""
        entry = self._by_key.get(key)
        if entry is None:
            return None
        return self._to_location(entry)

    def geocode(self, address: str) -> Location:
        """Resolve an address, biased to the service area and checked against it.

        Both halves are necessary and neither is sufficient. Without the bias, "2nd
        ave" typed during a call in Seattle resolves to 2nd Avenue, Manhattan, because
        a global geocoder ranks by prominence and has no idea where the vans are.
        Without the check, a bias that merely *prefers* nearby results still returns
        the far one when nothing closer matches.

        The failure that prompted this reached the solver, which asked the frozen
        travel snapshot for a leg to New York and raised a cache miss - so a
        dispatcher saw "Internal Server Error" for what was really "that address is
        2,400 miles away".
        """
        cached = self._by_query.get(address.strip().lower())
        if cached is not None:
            return self._to_location(cached)
        if not self._allow_network:
            raise GeocodeError(
                f"{address!r} is not in the geocode cache and network lookup is disabled"
            )
        entry = self._fetch(address)
        location = self._to_location(entry)
        self._check_in_area(address, location)
        self._by_query[address.strip().lower()] = entry
        return location

    def _check_in_area(self, address: str, location: Location) -> None:
        if self.near is None or self.radius_miles is None:
            return
        miles = self.near.haversine_miles(location)
        if miles > self.radius_miles:
            raise OutsideServiceArea(address, miles, self.radius_miles)

    def _viewbox(self) -> str | None:
        """The bounding box to search inside, as Nominatim wants it.

        A degree of latitude is about 69 miles everywhere; a degree of longitude
        shrinks with the cosine of latitude, which at the depot's 33 degrees leaves
        about 58 miles. Treating them as equal would make the box half again too
        narrow east to west, and clip addresses that are genuinely in range.
        """
        if self.near is None or self.radius_miles is None:
            return None
        lat_pad = self.radius_miles / 69.0
        lon_pad = self.radius_miles / (69.0 * max(0.1, math.cos(math.radians(self.near.lat))))
        return ",".join(
            str(round(value, 4))
            for value in (
                self.near.lon - lon_pad,
                self.near.lat + lat_pad,
                self.near.lon + lon_pad,
                self.near.lat - lat_pad,
            )
        )

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _to_location(entry: dict[str, object]) -> Location:
        return Location(
            lat=float(entry["lat"]),  # type: ignore[arg-type]
            lon=float(entry["lon"]),  # type: ignore[arg-type]
            address=str(entry.get("display_name", "")),
        )

    def _fetch(self, address: str) -> dict[str, object]:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < RATE_LIMIT_SECONDS:
                time.sleep(RATE_LIMIT_SECONDS - elapsed)
            self._last_request = time.monotonic()

        params: dict[str, str | int] = {"q": address, "format": "json", "limit": 1}
        viewbox = self._viewbox()
        if viewbox:
            # bounded=1 makes this a hard restriction rather than a preference. A
            # preference still returns Manhattan when nothing in Seattle matches, which
            # is the case that caused the bug.
            params["viewbox"] = viewbox
            params["bounded"] = 1
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(f"{ENDPOINT}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            raise GeocodeError(f"could not reach the geocoder for {address!r}: {exc}") from exc

        if not payload:
            where = " in the service area" if self._viewbox() else ""
            raise GeocodeError(f"no match for {address!r}{where}")
        hit = payload[0]
        return {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "display_name": hit["display_name"],
            "query": address,
        }
