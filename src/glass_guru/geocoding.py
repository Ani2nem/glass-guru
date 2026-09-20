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


class Geocoder:
    """Address to :class:`~glass_guru.domain.models.Location`, cache first."""

    def __init__(self, cache_path: Path | None = None, *, allow_network: bool = True) -> None:
        self.cache_path = cache_path or DEFAULT_CACHE
        self._allow_network = allow_network
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
        cached = self._by_query.get(address.strip().lower())
        if cached is not None:
            return self._to_location(cached)
        if not self._allow_network:
            raise GeocodeError(
                f"{address!r} is not in the geocode cache and network lookup is disabled"
            )
        entry = self._fetch(address)
        self._by_query[address.strip().lower()] = entry
        return self._to_location(entry)

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

        query = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
        request = urllib.request.Request(f"{ENDPOINT}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            raise GeocodeError(f"could not reach the geocoder for {address!r}: {exc}") from exc

        if not payload:
            raise GeocodeError(f"no match for {address!r}")
        hit = payload[0]
        return {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "display_name": hit["display_name"],
            "query": address,
        }
