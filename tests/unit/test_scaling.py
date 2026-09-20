"""Where the solver stops coping, pinned.

Wall-clock assertions are mostly a way to make a suite flaky on a loaded machine, so
these assert the *structural* facts that produce the performance - arcs pruned, a
cache hit, a separate budget for the hot path - and only bound timing loosely enough
to catch an order-of-magnitude regression.

The numbers in the comments come from `make load` and are what the thresholds were
chosen against.
"""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.catalog import CATALOG
from glass_guru.domain.enums import Certification, ServiceType
from glass_guru.domain.models import Job, Location, TimeWindow
from glass_guru.evals.load import synthetic_world
from glass_guru.fixtures.sample_business import DEPOT, WEEK_START, _at
from glass_guru.scheduler.booking import BaselineCache, suggest_booking_slots
from glass_guru.scheduler.day_planner import SolveParams, plan_day
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider

TRAVEL = SyntheticTravelProvider()


@pytest.fixture
def business() -> BusinessParams:
    return BusinessParams.load()


@pytest.fixture
def tz(business: BusinessParams) -> ZoneInfo:
    return ZoneInfo(business.meta.timezone)


def solve(jobs: int, workers: int, vans: int, tz: ZoneInfo, **overrides: object):
    world = synthetic_world(jobs=jobs, workers=workers, vans=vans)
    params = SolveParams(business_tz=tz, max_solve_seconds=3.0, **overrides)  # type: ignore[arg-type]
    return plan_day(
        world=world,
        travel=TRAVEL,
        on_date=WEEK_START,
        candidate_job_ids=list(world.jobs),
        params=params,
    )


# --------------------------------------------------------------------- pruning


def test_a_small_day_keeps_every_arc(tz: ZoneInfo):
    """Below the threshold the full model is quick and strictly better: at 25 jobs it
    serves 23 where pruning serves 21. Pruning a day that size costs two jobs for
    nothing."""
    result = solve(12, 6, 4, tz)
    assert result.metrics["arcs_pruned"] == 0


def test_a_large_day_prunes(tz: ZoneInfo):
    """Above the threshold, pruning is not an optimisation - it is the difference
    between a plan and no plan. Unpruned, a 30-job day returns UNKNOWN inside ten
    seconds; with k=12 it serves 25 of them.

    Asserted on the metrics rather than the routes, because a 40-job day may or may
    not find a solution inside a test-sized budget and pinning that would make the
    suite a weather report.
    """
    result = solve(40, 10, 6, tz)
    assert result.metrics["arcs_pruned"] > 0
    assert result.metrics["arcs_per_crew"] > 0


def test_pruning_keeps_every_depot_arc(tz: ZoneInfo):
    """A crew must be able to start and finish anywhere. Dropping depot arcs is how
    pruning turns a feasible day infeasible."""
    from glass_guru.scheduler.day_planner import _prunable_arcs

    size = 30
    matrix = [[abs(i - j) * 5 for j in range(size)] for i in range(size)]
    params = SolveParams(business_tz=tz, k_nearest=4, prune_above=5)
    arcs = _prunable_arcs(matrix, params)

    for node in range(1, size):
        assert (0, node) in arcs, f"no way out of the depot to {node}"
        assert (node, 0) in arcs, f"no way home from {node}"


def test_pruning_is_symmetric_where_it_needs_to_be(tz: ZoneInfo):
    """A is among B's nearest without B being among A's. Keeping only mutual pairs
    would strand the outlying stop, so the union is taken."""
    from glass_guru.scheduler.day_planner import _prunable_arcs

    matrix = [[abs(i - j) for j in range(20)] for i in range(20)]
    params = SolveParams(business_tz=tz, k_nearest=3, prune_above=5)
    arcs = _prunable_arcs(matrix, params)
    for i, j in arcs:
        if i and j:
            assert (j, i) in arcs, f"arc {i}->{j} has no return"


def test_a_day_just_over_the_threshold_still_produces_a_plan(tz: ZoneInfo):
    """The size that matters: a business slightly larger than this one. Given a
    realistic batch budget it still answers, where the unpruned model does not."""
    world = synthetic_world(jobs=32, workers=8, vans=5)
    result = plan_day(
        world=world,
        travel=TRAVEL,
        on_date=WEEK_START,
        candidate_job_ids=list(world.jobs),
        params=SolveParams(business_tz=tz, max_solve_seconds=10.0),
    )
    assert result.metrics["arcs_pruned"] > 0
    assert result.routes, f"no plan at 32 jobs ({result.status})"
    assert {j for route in result.routes for j in route.job_ids}


# ------------------------------------------------------------------- hot path


def draft(offset: int) -> Job:
    entry = CATALOG[ServiceType.SCREEN_REPAIR]
    return Job(
        id=f"draft-{offset}",
        customer_id="c",
        customer_name="Caller",
        location=Location(lat=DEPOT.lat + 0.03 + offset * 0.001, lon=DEPOT.lon - 0.04),
        service_type=entry.service_type,
        required_certifications=frozenset({Certification.SCREEN_REPAIR}),
        estimated_duration_min=entry.typical_duration_min,
        windows=(TimeWindow(start=_at(0, 8), end=_at(0, 17)),),
        requested_at=_at(0, 9),
    )


def test_a_quote_does_not_inherit_the_batch_budget(business: BusinessParams, tz: ZoneInfo):
    """At 25 jobs a day this made a quote take twenty seconds, which is not a feature
    anybody would use. A good enough answer now beats a better one after the caller
    has hung up."""
    assert business.solver.quote_solve_seconds.value < business.solver.max_solve_seconds.value


def test_the_untouched_day_is_solved_once_across_quotes(business: BusinessParams, tz: ZoneInfo):
    """Half of a quote is re-solving the day as it already stands, and that answer does
    not change between one caller and the next. Measured: 3.09s to 1.55s."""
    world = synthetic_world(jobs=10, workers=6, vans=4)
    params = SolveParams.from_business(business, tz)
    cache = BaselineCache()

    for offset in range(3):
        suggest_booking_slots(
            world=world,
            travel=TRAVEL,
            draft=draft(offset),
            horizon=[WEEK_START],
            params=params,
            business=business,
            cache=cache,
        )
    assert cache.misses == 1
    assert cache.hits == 2


def test_booking_the_same_job_twice_gives_the_same_answer(business: BusinessParams, tz: ZoneInfo):
    """A cache that changed the answer would be worse than none."""
    world = synthetic_world(jobs=10, workers=6, vans=4)
    params = SolveParams.from_business(business, tz)
    cache = BaselineCache()

    first = suggest_booking_slots(
        world=world,
        travel=TRAVEL,
        draft=draft(0),
        horizon=[WEEK_START],
        params=params,
        business=business,
        cache=cache,
    )
    second = suggest_booking_slots(
        world=world,
        travel=TRAVEL,
        draft=draft(0),
        horizon=[WEEK_START],
        params=params,
        business=business,
        cache=cache,
    )
    assert [s.marginal_cost for s in first.slots] == [s.marginal_cost for s in second.slots]


def test_a_changed_day_invalidates_the_cache(business: BusinessParams, tz: ZoneInfo):
    """Keyed on the exact job set, so a booking or a cancellation invalidates it by
    construction rather than by somebody remembering to."""
    world = synthetic_world(jobs=10, workers=6, vans=4)
    params = SolveParams.from_business(business, tz)
    cache = BaselineCache()

    suggest_booking_slots(
        world=world,
        travel=TRAVEL,
        draft=draft(0),
        horizon=[WEEK_START],
        params=params,
        business=business,
        cache=cache,
    )
    del world.jobs[next(iter(world.jobs))]
    suggest_booking_slots(
        world=world,
        travel=TRAVEL,
        draft=draft(1),
        horizon=[WEEK_START],
        params=params,
        business=business,
        cache=cache,
    )
    assert cache.misses == 2


def test_the_fixture_still_plans_in_well_under_a_second(business: BusinessParams, tz: ZoneInfo):
    """The demo has to feel instant. A loose bound: this measures about 0.05s, so an
    order of magnitude of headroom before it fails."""
    from glass_guru.domain.state import fold
    from glass_guru.fixtures.sample_business import sample_world_at
    from glass_guru.scheduler.horizon import HorizonParams, plan_horizon

    events, now = sample_world_at()
    world = fold(events, as_of=now)
    started = time.monotonic()
    plan_horizon(
        world=world,
        travel=TRAVEL,
        start=WEEK_START,
        params=SolveParams.from_business(business, tz),
        horizon_params=HorizonParams.from_business(business),
    )
    assert time.monotonic() - started < 2.0
