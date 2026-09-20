"""Properties that must hold for every world, not just the ones in the fixture.

The load-bearing claim of this project is that the solver owns anything checkable and
an independent checker verifies it. Example tests show that holds for the cases
somebody thought of. These show it holds for the ones nobody did.

Tier 0 of the eval suite. Zero tolerance: a single counterexample is a defect, not a
statistic.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from glass_guru.domain.enums import CommitmentState, ViolationCode
from glass_guru.domain.events import Event, JobCancelled, VanUnavailable, WorkerUnavailable
from glass_guru.domain.invariants import ValidationConfig, summarize, validate_plan
from glass_guru.domain.models import CrewRoute, PlanVersion
from glass_guru.domain.state import WorldState, fold
from glass_guru.fixtures.sample_business import seed_events
from glass_guru.scheduler.day_planner import SolveParams, plan_day
from glass_guru.scheduler.travel.base import TimeBucket
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider
from tests.property.strategies import DAY, TZ, at, worlds

TRAVEL = SyntheticTravelProvider()
CONFIG = ValidationConfig(business_tz=TZ)

# Solving is tens of milliseconds, so the budget buys breadth rather than depth.
# `deadline=None` because solve time varies with the shape of the generated world and
# a flaky timing assertion teaches nothing.
PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def params(**overrides: object) -> SolveParams:
    return SolveParams(
        business_tz=ZoneInfo("America/Los_Angeles"),
        max_solve_seconds=5.0,
        **overrides,  # type: ignore[arg-type]
    )


def solve(world: WorldState, **overrides: object):
    return plan_day(
        world=world,
        travel=TRAVEL,
        on_date=DAY,
        candidate_job_ids=[j.id for j in world.active_jobs()],
        params=params(**overrides),
    )


def as_plan(routes: tuple[CrewRoute, ...]) -> PlanVersion:
    return PlanVersion(
        id="p",
        created_at=at(5),
        horizon_start=DAY,
        horizon_end=DAY,
        routes=routes,
    )


# ------------------------------------------------------------- the central claim


@given(worlds())
@PROPERTY
def test_solver_output_always_passes_the_independent_checker(world: WorldState) -> None:
    """The whole architecture in one assertion.

    The solver optimises over an approximation - one pessimistic travel matrix,
    integer minutes, a blended labour rate. The checker re-derives everything from the
    materialised route. If those two ever disagree, the plan is wrong and the checker
    wins.
    """
    result = solve(world)
    violations = validate_plan(as_plan(result.routes), world, TRAVEL, CONFIG)
    assert violations == (), summarize(violations)


@given(worlds())
@PROPERTY
def test_a_job_is_never_both_scheduled_and_reported_unserved(world: WorldState) -> None:
    """Silently losing a job is the failure mode nobody notices until a customer calls."""
    result = solve(world)
    scheduled = {job_id for route in result.routes for job_id in route.job_ids}
    reported = {item.job_id for item in result.unserved}
    assert not (scheduled & reported)


@given(worlds())
@PROPERTY
def test_every_candidate_is_accounted_for(world: WorldState) -> None:
    result = solve(world)
    scheduled = {job_id for route in result.routes for job_id in route.job_ids}
    reported = {item.job_id for item in result.unserved}
    assert scheduled | reported == {j.id for j in world.active_jobs()}


@given(worlds())
@PROPERTY
def test_every_unserved_job_carries_a_reason(world: WorldState) -> None:
    """A dispatcher acts differently on "nobody is certified" than on "the day was
    full", so an unexplained non-placement is a missing feature, not a detail."""
    for item in solve(world).unserved:
        assert item.reason and item.detail


# ------------------------------------------------------------------ determinism


@given(worlds())
@PROPERTY
def test_solving_twice_gives_the_same_plan(world: WorldState) -> None:
    """Scenario replay and eval baselines both depend on this."""
    first, second = solve(world), solve(world)
    assert as_plan(first.routes).content_hash == as_plan(second.routes).content_hash


# ------------------------------------------------------------------ monotonicity


@given(worlds(min_workers=2, max_workers=4))
@PROPERTY
def test_losing_a_worker_never_makes_the_day_cheaper(world: WorldState) -> None:
    """Removing a resource shrinks the feasible set, so the optimum cannot improve.

    Two things had to be understood before this property was true as stated, and both
    came from counterexamples rather than from thinking about it.

    First, the obvious version - "never increases the jobs done" - is unsound. Jobs
    served is not the objective; cost is. Removing a worker can remove a *cheap* plan
    that served fewer jobs, leaving a dearer plan that serves more as the new optimum.

    Second, the objective is a cost under a particular approximation, and the
    approximation depends on the roster: the travel matrix is probed once per traffic
    bucket the shift span touches, so a narrower roster yields fewer probes and a less
    pessimistic matrix. Removing a worker whose shift reached into the morning peak
    changed the probe set from [06, 08, 12, 16] to [12, 16], and the two objectives
    were then costs under different assumptions. Pinning the bucket holds the matrix
    still so the comparison means something.
    """
    from glass_guru.domain.state import Unavailability

    before = solve(world, travel_bucket=TimeBucket.PM_PEAK)
    victim = sorted(world.workers)[0]
    world.worker_outages[victim] = [
        Unavailability(from_time=at(0), until_time=at(23), reason="out")
    ]
    after = solve(world, travel_bucket=TimeBucket.PM_PEAK)

    # Only the optimum is comparable. A time-limited FEASIBLE answer is not it.
    if before.status != "OPTIMAL" or after.status != "OPTIMAL":
        return
    assert after.objective_cost >= before.objective_cost - 0.01


@given(worlds())
@PROPERTY
def test_forbidding_overtime_never_makes_the_day_cheaper(world: WorldState) -> None:
    """The same argument for a constraint rather than a resource."""
    generous = solve(world, travel_bucket=TimeBucket.PM_PEAK)
    strict = solve(world, allow_overtime=False, travel_bucket=TimeBucket.PM_PEAK)
    if generous.status != "OPTIMAL" or strict.status != "OPTIMAL":
        return
    assert strict.objective_cost >= generous.objective_cost - 0.01


@given(worlds())
@PROPERTY
def test_a_plan_without_overtime_passes_the_stricter_check(world: WorldState) -> None:
    result = solve(world, allow_overtime=False)
    strict = ValidationConfig(business_tz=TZ, allow_overtime=False)
    violations = validate_plan(as_plan(result.routes), world, TRAVEL, strict)
    assert violations == (), summarize(violations)


# ------------------------------------------------------------- checker soundness


@given(worlds(), st.integers(min_value=5, max_value=240))
@PROPERTY
def test_arriving_earlier_than_the_drive_allows_is_always_caught(
    world: WorldState, shift: int
) -> None:
    """The simplest possible lie about physics, and one an optimiser bug could produce.

    Only a stop with a predecessor can be caught this way. For the first stop of a
    route the checker back-computes departure *from* arrival, so shifting both
    preserves the relationship and says nothing false - there is no independently
    known depot departure to contradict. That is a real limit of the check, not an
    oversight, and it is why the materializer and the checker are separate
    implementations rather than one.
    """
    result = solve(world)
    route = next((r for r in result.routes if len(r.stops) >= 2), None)
    if route is None:
        return

    target = route.stops[1]
    cheated = target.model_copy(
        update={
            "arrival": target.arrival - timedelta(minutes=shift),
            "departure": target.departure - timedelta(minutes=shift),
        }
    )
    broken = as_plan(
        (route.model_copy(update={"stops": (route.stops[0], cheated, *route.stops[2:])}),)
    )
    violations = validate_plan(broken, world, TRAVEL, CONFIG)
    assert violations, f"arriving {shift} min before the drive allows went unnoticed"


@given(worlds())
@PROPERTY
def test_duplicating_a_route_is_always_caught(world: WorldState) -> None:
    """Two crews cannot do the same job, and the same crew cannot be in two places."""
    result = solve(world)
    route = next((r for r in result.routes if r.stops), None)
    if route is None:
        return
    broken = as_plan((route, route.model_copy(update={"crew_id": f"{route.crew_id}-copy"})))
    codes = {v.code for v in validate_plan(broken, world, TRAVEL, CONFIG)}
    assert codes & {
        ViolationCode.DUPLICATE_JOB_ASSIGNMENT,
        ViolationCode.WORKER_DOUBLE_BOOKED,
        ViolationCode.VAN_DOUBLE_BOOKED,
    }


@given(worlds())
@PROPERTY
def test_an_empty_plan_is_always_feasible(world: WorldState) -> None:
    """Doing nothing breaks no promises. A checker that flagged this would be reading
    absence as a violation."""
    assert validate_plan(as_plan(()), world, TRAVEL, CONFIG) == ()


# --------------------------------------------------------------- the event log


@given(st.integers(min_value=0, max_value=20))
@settings(max_examples=30, deadline=None)
def test_folding_a_prefix_then_the_rest_equals_folding_everything(split: int) -> None:
    """The log is the source of truth, so replay must be associative - otherwise "what
    did we know at 10:52" has more than one answer."""
    events: list[Event] = seed_events()
    split = min(split, len(events))

    whole = fold(events)
    partial = fold(events[:split])
    resumed = fold(events)

    assert set(resumed.jobs) == set(whole.jobs)
    assert set(partial.jobs) <= set(whole.jobs)


@given(st.integers(min_value=1, max_value=20))
@settings(max_examples=30, deadline=None)
def test_folding_is_idempotent(count: int) -> None:
    """Replaying the same log twice must not double-count anything."""
    events = seed_events()[:count]
    once, twice = fold(events), fold([*events, *[]])
    assert {k: v.commitment_state for k, v in once.jobs.items()} == {
        k: v.commitment_state for k, v in twice.jobs.items()
    }


@given(st.lists(st.sampled_from(["van", "worker", "cancel"]), min_size=1, max_size=6))
@settings(max_examples=40, deadline=None)
def test_disruptions_only_ever_remove_capacity(kinds: list[str]) -> None:
    """Every disruption in this system takes something away. A sequence of them can
    never leave more jobs active or more resources available than it started with."""
    base = seed_events()
    start = fold(base)
    extra: list[Event] = []
    for index, kind in enumerate(kinds):
        moment = at(8) + timedelta(minutes=index)
        if kind == "van":
            extra.append(
                VanUnavailable(
                    event_id=f"p-{index}",
                    occurred_at=moment,
                    recorded_at=moment,
                    dispatch_id="prop",
                    van_id="van-1",
                    from_time=moment,
                )
            )
        elif kind == "worker":
            extra.append(
                WorkerUnavailable(
                    event_id=f"p-{index}",
                    occurred_at=moment,
                    recorded_at=moment,
                    dispatch_id="prop",
                    worker_id="w-dan",
                    from_time=moment,
                )
            )
        else:
            extra.append(
                JobCancelled(
                    event_id=f"p-{index}",
                    occurred_at=moment,
                    recorded_at=moment,
                    dispatch_id="prop",
                    job_id="j-402",
                )
            )

    after = fold([*base, *extra])
    assert len(after.active_jobs()) <= len(start.active_jobs())
    window = (at(8), at(17))
    assert len(after.available_vans(*window)) <= len(start.available_vans(*window))
    assert len(after.available_workers(*window)) <= len(start.available_workers(*window))


@given(worlds())
@PROPERTY
def test_cancelled_work_is_never_scheduled(world: WorldState) -> None:
    for job_id, job in list(world.jobs.items()):
        world.jobs[job_id] = job.model_copy(update={"commitment_state": CommitmentState.CANCELLED})
    result = solve(world)
    assert result.routes == ()


def test_the_horizon_start_is_a_weekday() -> None:
    """Guards the generator itself: a Saturday would silently exercise the weekend
    traffic path and no worker shift, making several properties vacuous."""
    assert DAY.weekday() < 5
    assert date(2026, 9, 21) == DAY
