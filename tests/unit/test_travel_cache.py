"""Travel cache behaviour.

The cache is what makes the economics work: a drive time is a property of two
neighbourhoods and a time bucket, not of two exact addresses and a timestamp.
These tests pin that collapsing, and pin the refuse-on-miss behaviour that keeps
frozen replay honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.models import Location
from glass_guru.domain.travel import TravelLeg
from glass_guru.fixtures.sample_business import BUSINESS_TZ, DEPOT, JOBS, _at
from glass_guru.scheduler.travel.cache import (
    CacheMiss,
    CachingTravelProvider,
    JsonLegStore,
    LegKey,
    MemoryLegStore,
    MissPolicy,
)
from glass_guru.scheduler.travel.factory import TravelMode, build_travel
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider


class CountingProvider:
    """Wraps synthetic travel and counts how often it is actually consulted."""

    def __init__(self) -> None:
        self._inner = SyntheticTravelProvider()
        self.calls = 0

    def leg(self, origin: Location, dest: Location, depart_at: datetime) -> TravelLeg:
        self.calls += 1
        return self._inner.leg(origin, dest, depart_at)


@pytest.fixture
def counting() -> CountingProvider:
    return CountingProvider()


# ----------------------------------------------------------------------- the key


def test_nearby_addresses_share_a_cache_entry():
    """Two points in the same ~150m cell have the same drive time to within noise.
    Collapsing them is what keeps the cache small enough to be free."""
    a = Location(lat=47.6205, lon=-122.3493)
    b = Location(lat=47.6206, lon=-122.3494)
    at = _at(0, 9)
    assert LegKey.build(a, DEPOT, at) == LegKey.build(b, DEPOT, at)


def test_times_in_the_same_bucket_share_a_cache_entry():
    a, b = _at(0, 8, 14), _at(0, 8, 31)
    assert LegKey.build(DEPOT, JOBS[0].location, a) == LegKey.build(DEPOT, JOBS[0].location, b)


def test_different_buckets_do_not_share_an_entry():
    """Rush hour is a different drive from midday; collapsing those would be wrong."""
    morning, midday = _at(0, 8), _at(0, 12)
    assert LegKey.build(DEPOT, JOBS[0].location, morning) != LegKey.build(
        DEPOT, JOBS[0].location, midday
    )


def test_weekday_and_weekend_do_not_share_an_entry():
    weekday, saturday = _at(0, 8), _at(5, 8)
    assert LegKey.build(DEPOT, JOBS[0].location, weekday) != LegKey.build(
        DEPOT, JOBS[0].location, saturday
    )


def test_direction_matters():
    """One-ways and turn restrictions make A->B and B->A genuinely different."""
    at = _at(0, 9)
    assert LegKey.build(DEPOT, JOBS[0].location, at) != LegKey.build(JOBS[0].location, DEPOT, at)


def test_key_round_trips_through_its_encoding():
    key = LegKey.build(DEPOT, JOBS[0].location, _at(0, 9))
    assert LegKey.decode(key.encode()) == key


# -------------------------------------------------------------------- behaviour


def test_repeat_lookups_consult_the_provider_once(counting: CountingProvider):
    cache = CachingTravelProvider(counting)
    for minute in range(0, 40, 7):
        cache.leg(DEPOT, JOBS[0].location, _at(0, 8, minute))
    assert counting.calls == 1
    assert cache.stats.hits == 5


def test_a_re_solve_costs_nothing(counting: CountingProvider):
    """The geography does not change when a van breaks down, so a repair must not
    generate a single new lookup."""
    cache = CachingTravelProvider(counting)
    places = [DEPOT, *(j.location for j in JOBS[:5])]
    for origin in places:
        for dest in places:
            if origin is not dest:
                cache.leg(origin, dest, _at(0, 9))
    first_pass = counting.calls

    for origin in places:
        for dest in places:
            if origin is not dest:
                cache.leg(origin, dest, _at(0, 9, 25))
    assert counting.calls == first_pass, "a repair triggered new travel lookups"


def test_frozen_snapshot_refuses_unknown_legs():
    """A frozen run that quietly reached the network would be neither reproducible
    nor free, and would not announce itself."""
    cache = CachingTravelProvider(None, MemoryLegStore(), on_miss=MissPolicy.ERROR)
    with pytest.raises(CacheMiss, match="frozen travel snapshot"):
        cache.leg(DEPOT, JOBS[0].location, _at(0, 9))


def test_compute_policy_requires_a_provider():
    with pytest.raises(ValueError, match="provider is required"):
        CachingTravelProvider(None, on_miss=MissPolicy.COMPUTE)


def test_matrix_diagonal_is_zero(counting: CountingProvider):
    cache = CachingTravelProvider(counting)
    keyed = [("depot", DEPOT)] + [(j.id, j.location) for j in JOBS[:3]]
    matrix = cache.matrix(keyed, _at(0, 9))
    assert all(matrix.minutes[i][i] == 0 for i in range(len(matrix.keys)))


def test_json_store_round_trips(tmp_path):
    path = tmp_path / "legs.json"
    store = JsonLegStore(path)
    key = LegKey.build(DEPOT, JOBS[0].location, _at(0, 9))
    store.put(key, TravelLeg(minutes=17, miles=4.25))
    assert store.flush() is True
    assert store.flush() is False, "a clean store should not rewrite the file"

    reloaded = JsonLegStore(path)
    assert reloaded.get(key) == TravelLeg(minutes=17, miles=4.25)


# ---------------------------------------------------------------------- factory


def test_frozen_snapshot_covers_the_whole_fixture():
    """Every leg a solve can ask for must be present, or scenarios break offline."""
    travel = build_travel(BusinessParams.load(), TravelMode.FROZEN)
    places = [DEPOT, *(j.location for j in JOBS)]
    probes = [_at(0, h) for h in (6, 8, 12, 16, 19)]
    for origin in places:
        for dest in places:
            if origin.geohash7 == dest.geohash7:
                continue
            for probe in probes:
                travel.leg(origin, dest, probe)


def test_real_roads_disagree_with_straight_lines():
    """The point of OSRM. Chen Residence is across the ship canal: the straight-line
    estimate is shorter in distance but the bridge makes it longer in time, which no
    haversine model can know."""
    business = BusinessParams.load()
    frozen = build_travel(business, TravelMode.FROZEN)
    synthetic = build_travel(business, TravelMode.SYNTHETIC)
    chen = next(j for j in JOBS if j.id == "j-402")
    at = _at(0, 12)

    real = frozen.leg(DEPOT, chen.location, at)
    approx = synthetic.leg(DEPOT, chen.location, at)
    assert real.miles < approx.miles
    assert real.minutes > approx.minutes


def test_auto_mode_prefers_the_snapshot():
    business = BusinessParams.load()
    auto = build_travel(business, TravelMode.AUTO)
    assert isinstance(auto, CachingTravelProvider)


def test_missing_snapshot_explains_how_to_build_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="freeze_travel"):
        build_travel(BusinessParams.load(), TravelMode.FROZEN, snapshot=tmp_path / "absent.json")


def test_bucket_boundaries_are_stable_across_a_day():
    """Sanity on the bucket function itself: a working day touches every bucket
    exactly where expected, which is what the snapshot's probe times rely on."""
    from glass_guru.scheduler.travel.base import TimeBucket, bucket_for

    day = datetime(2026, 9, 21, 0, 0, tzinfo=BUSINESS_TZ)
    seen = [bucket_for(day + timedelta(hours=h)) for h in (6, 8, 12, 16, 19)]
    assert seen == [
        TimeBucket.EARLY,
        TimeBucket.AM_PEAK,
        TimeBucket.MIDDAY,
        TimeBucket.PM_PEAK,
        TimeBucket.EVENING,
    ]
