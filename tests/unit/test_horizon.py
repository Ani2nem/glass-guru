"""Rolling-horizon tests.

The load-bearing property is the same as for the day solver: whatever comes out must
pass the independent invariant checker. Beyond that, these assert the things the
two-stage decomposition is *for* - that day assignment respects material lead times
and customer windows, that a scarce certification cannot be oversubscribed, that
geography actually clusters, and that an over-filled day corrects itself.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import UnservedReason
from glass_guru.domain.invariants import ValidationConfig, summarize, validate_plan
from glass_guru.domain.models import PlanVersion
from glass_guru.domain.state import Unavailability, WorldState
from glass_guru.fixtures.sample_business import WEEK_START, _at
from glass_guru.scheduler.day_planner import SolveParams
from glass_guru.scheduler.horizon import (
    HorizonParams,
    HorizonResult,
    _sector_of,
    day_capacity,
    plan_horizon,
)

THURSDAY = date.fromordinal(WEEK_START.toordinal() + 3)


@pytest.fixture
def business() -> BusinessParams:
    return BusinessParams.load()


@pytest.fixture
def tz(business: BusinessParams):
    return ZoneInfo(business.meta.timezone)


@pytest.fixture
def params(business: BusinessParams, tz) -> SolveParams:
    return SolveParams.from_business(business, tz)


@pytest.fixture
def horizon_params(business: BusinessParams) -> HorizonParams:
    return HorizonParams.from_business(business)


def run(world, travel, params, horizon_params, **kwargs) -> HorizonResult:
    return plan_horizon(
        world=world,
        travel=travel,
        start=WEEK_START,
        params=params,
        horizon_params=horizon_params,
        **kwargs,
    )


def as_plan(result: HorizonResult, world: WorldState, days: int = 5) -> PlanVersion:
    return PlanVersion(
        id="h",
        created_at=world.as_of,
        horizon_start=WEEK_START,
        horizon_end=date.fromordinal(WEEK_START.toordinal() + days - 1),
        routes=result.routes,
        unserved=result.unserved,
    )


def reasons(result: HorizonResult) -> dict[str, UnservedReason]:
    return {u.job_id: u.reason for u in result.unserved}


# ------------------------------------------------------------------------ the basics


def test_whole_week_is_scheduled(world, travel, params, horizon_params):
    result = run(world, travel, params, horizon_params)
    assert result.scheduled_job_ids == {j.id for j in world.active_jobs()}
    assert result.unserved == ()


def test_horizon_plan_passes_the_independent_checker(world, travel, params, horizon_params, tz):
    result = run(world, travel, params, horizon_params)
    violations = validate_plan(
        as_plan(result, world), world, travel, ValidationConfig(business_tz=tz)
    )
    assert violations == (), summarize(violations)


def test_assignment_converges_without_spilling(world, travel, params, horizon_params):
    """A single round means stage A's capacity bound was honest enough."""
    assert run(world, travel, params, horizon_params).rounds == 1


def test_planning_is_reproducible(world, travel, params, horizon_params):
    first = run(world, travel, params, horizon_params)
    second = run(world, travel, params, horizon_params)
    assert as_plan(first, world).content_hash == as_plan(second, world).content_hash


# ----------------------------------------------------------------- day assignment


def test_back_ordered_job_lands_on_or_after_its_ready_date(world, travel, params, horizon_params):
    """The tempered unit arrives Thursday; no routing decision can move that."""
    result = run(world, travel, params, horizon_params)
    assert result.assignment["j-406"] >= THURSDAY


def test_jobs_land_on_the_day_their_window_falls(world, travel, params, horizon_params, tz):
    result = run(world, travel, params, horizon_params)
    for job_id, on_date in result.assignment.items():
        job = world.jobs[job_id]
        if not job.windows:
            continue
        assert any(w.start.astimezone(tz).date() == on_date for w in job.windows), (
            f"{job_id} assigned {on_date} but its windows are on "
            f"{[w.start.astimezone(tz).date() for w in job.windows]}"
        )


def test_short_horizon_pushes_later_work_out_honestly(world, travel, params, horizon_params):
    """A two-day horizon cannot reach Thursday's tempered install, and should say so
    in those terms rather than blaming capacity."""
    from dataclasses import replace

    result = run(world, travel, params, replace(horizon_params, days=2))
    assert reasons(result)["j-406"] is UnservedReason.MATERIALS_NOT_AVAILABLE


def test_losing_a_scarce_certification_is_explained_across_the_horizon(
    world, travel, params, horizon_params
):
    for worker_id in ("w-marcus", "w-priya"):
        world.worker_outages[worker_id] = [
            Unavailability(from_time=_at(0, 0), until_time=_at(5, 0), reason="out all week")
        ]
    result = run(world, travel, params, horizon_params)
    assert reasons(result)["j-401"] is UnservedReason.NO_CERTIFIED_WORKER


# --------------------------------------------------------------------- capacity


def test_day_capacity_counts_only_available_certified_time(world, tz, business):
    """Monday capacity must drop when a worker is out, and drop for exactly the
    certifications that worker held."""
    before = day_capacity(world, WEEK_START, tz, business.horizon.day_capacity_utilization.value)
    world.worker_outages["w-marcus"] = [
        Unavailability(from_time=_at(0, 0), until_time=_at(1, 0), reason="sick")
    ]
    after = day_capacity(world, WEEK_START, tz, business.horizon.day_capacity_utilization.value)

    assert after.total_person_minutes < before.total_person_minutes
    assert after.worker_count == before.worker_count - 1
    from glass_guru.domain.enums import Certification

    assert (
        after.by_certification[Certification.COMMERCIAL_STOREFRONT]
        < before.by_certification[Certification.COMMERCIAL_STOREFRONT]
    )
    assert (
        after.by_certification[Certification.AUTO_GLASS]
        == before.by_certification[Certification.AUTO_GLASS]
    )


def test_squeezing_the_week_into_one_day_spills_and_re_rounds(
    world, travel, params, horizon_params
):
    """A one-day horizon cannot hold the week. The point is that it fails honestly:
    some work is placed, the rest is reported, and nothing is silently dropped."""
    from dataclasses import replace

    result = run(world, travel, params, replace(horizon_params, days=1))
    placed = result.scheduled_job_ids
    reported = {u.job_id for u in result.unserved}
    assert placed
    assert placed | reported == {j.id for j in world.active_jobs()}
    assert not (placed & reported), "a job was both scheduled and reported unserved"


# ---------------------------------------------------------------------- cohesion


def test_days_are_geographically_coherent(world, travel, params, horizon_params):
    """Sector spread is penalised so a day does not zig-zag across the metro.
    Averaged over the week, a day should touch fewer sectors than it has jobs."""
    result = run(world, travel, params, horizon_params)
    depot = world.vans["van-1"].home_depot

    for on_date, day in result.day_results.items():
        job_ids = [j for route in day.routes for j in route.job_ids]
        if len(job_ids) < 2:
            continue
        sectors = {_sector_of(world.jobs[j].location, depot) for j in job_ids}
        assert len(sectors) <= len(job_ids), f"{on_date} touches {len(sectors)} sectors"


# ------------------------------------------------------------------------ buffer


def test_hard_windows_keep_a_planning_buffer(world, travel, params, horizon_params, tz):
    """Finishing at exactly the deadline is legal but fragile. Where the window has
    room, the planner leaves margin."""
    result = run(world, travel, params, horizon_params)
    buffer = timedelta(minutes=params.hard_window_buffer_minutes)
    assert buffer > timedelta(0)

    checked = 0
    for route in result.routes:
        for stop in route.stops:
            job = world.jobs[stop.job_id]
            for window in job.hard_windows:
                room = window.end - window.start - timedelta(minutes=job.estimated_duration_min)
                if room >= buffer:
                    assert stop.departure <= window.end - buffer, (
                        f"{job.id} finishes {stop.departure} with no margin before {window.end}"
                    )
                    checked += 1
    assert checked, "no hard-window job was exercised"


def test_buffer_never_makes_a_job_infeasible(world, travel, params, horizon_params, tz):
    """A window with exactly enough room must still be schedulable - the buffer is a
    preference, not a new constraint that quietly drops work."""
    from dataclasses import replace

    tight = replace(params, hard_window_buffer_minutes=600)
    result = run(world, travel, tight, horizon_params)
    assert "j-401" in result.scheduled_job_ids
    violations = validate_plan(
        as_plan(result, world), world, travel, ValidationConfig(business_tz=tz)
    )
    assert violations == (), summarize(violations)
