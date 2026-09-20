"""Rolling multi-day planning: which day does each job land on, and then what happens
on that day.

Two stages, because one monolithic model over five days and 125 stops is both slow
and harder to reason about:

**Stage A - assign_days.** A coarse CP-SAT model choosing a day per job. It knows
about material lead times, which days a customer window actually touches, whether
anyone certified is working, roughly how many crew-hours each day has, and how
geographically scattered a day would be. It knows nothing about routing.

**Stage B - plan_day.** The exact model, run once per day on that day's jobs.

Then they iterate. Stage A's capacity model is a coarse bound, so a day can be handed
more work than it can actually route. When that happens the spilled jobs come back,
that day's capacity estimate is tightened by what it genuinely absorbed, and Stage A
runs again. Two or three rounds converge; the loop stops early when nothing spills.

This decomposition also mirrors how the business already thinks - "Thursday is the
north side" is a day-assignment decision, and it is made before anyone works out a
driving order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from ortools.sat.python import cp_model

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import Certification, UnservedReason
from glass_guru.domain.models import (
    CrewRoute,
    Job,
    JobId,
    Location,
    UnservedJob,
    Worker,
)
from glass_guru.domain.state import WorldState
from glass_guru.domain.travel import TravelOracle
from glass_guru.scheduler.day_planner import (
    DayPlanResult,
    SolveParams,
    _cents,
    _largest_free_interval,
    _shift_window,
    _unserved_penalty,
    plan_day,
)

#: Compass sectors around the depot, used as the geographic-cohesion proxy.
SECTORS = 8


@dataclass(frozen=True, slots=True)
class HorizonParams:
    days: int = 5
    capacity_utilization: float = 0.65
    sector_spread_penalty: float = 45.0
    day_delay_penalty: float = 12.0
    max_rounds: int = 3

    @classmethod
    def from_business(cls, business: BusinessParams) -> HorizonParams:
        return cls(
            days=int(business.horizon.days.value),
            capacity_utilization=business.horizon.day_capacity_utilization.value,
            sector_spread_penalty=business.horizon.sector_spread_penalty.value,
            day_delay_penalty=business.horizon.day_delay_penalty.value,
        )


@dataclass(frozen=True, slots=True)
class DayCapacity:
    """Coarse crew-minutes available on one day, overall and per certification."""

    on_date: date
    total_person_minutes: int
    by_certification: dict[Certification, int]
    worker_count: int

    def for_job(self, job: Job) -> int:
        """Binding capacity for this job: the scarcest certification it needs."""
        if not job.required_certifications:
            return self.total_person_minutes
        return min(self.by_certification.get(c, 0) for c in job.required_certifications)


@dataclass(frozen=True, slots=True)
class HorizonResult:
    routes: tuple[CrewRoute, ...]
    unserved: tuple[UnservedJob, ...]
    day_results: dict[date, DayPlanResult]
    assignment: dict[JobId, date]
    rounds: int
    #: Promises broken across the horizon, so a caller can authorise them.
    released_promises: tuple[JobId, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def scheduled_job_ids(self) -> set[JobId]:
        return {job_id for route in self.routes for job_id in route.job_ids}


# --------------------------------------------------------------------------- helpers


def _dates(start: date, days: int) -> list[date]:
    return [date.fromordinal(start.toordinal() + offset) for offset in range(days)]


def _day_start(on_date: date, tz: tzinfo) -> datetime:
    return datetime.combine(on_date, time(0, 0), tzinfo=tz)


def _sector_of(location: Location, depot: Location) -> int:
    """Which compass sector around the depot this job sits in.

    A crude but legible cohesion signal: penalising the number of sectors a day
    touches is what produces "north side Thursday" rather than a day that zig-zags
    across the metro. Jobs essentially at the depot get sector 0 so they never add
    spread on their own.
    """
    dy = location.lat - depot.lat
    dx = (location.lon - depot.lon) * math.cos(math.radians(depot.lat))
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0
    bearing = math.atan2(dx, dy)
    return int((bearing + math.pi) / (2 * math.pi) * SECTORS) % SECTORS


def day_capacity(world: WorldState, on_date: date, tz: tzinfo, utilization: float) -> DayCapacity:
    """Crew-minutes plausibly available, after allowing for driving and slack.

    Deliberately an upper bound rather than a routing estimate - Stage B decides what
    actually fits, and the iteration exists to correct this when it is too generous.
    """
    day_begins = _day_start(on_date, tz)
    total = 0
    by_cert: dict[Certification, int] = dict.fromkeys(Certification, 0)
    count = 0

    for worker in world.workers.values():
        span = _shift_window(worker, on_date)
        if span is None:
            continue
        usable = _largest_free_interval(
            world.worker_outages.get(worker.id, ()), day_begins, span[0], span[1]
        )
        if usable is None:
            continue
        minutes = int((usable[1] - usable[0]) * utilization)
        total += minutes
        count += 1
        for cert in worker.certifications:
            by_cert[cert] += minutes

    return DayCapacity(
        on_date=on_date, total_person_minutes=total, by_certification=by_cert, worker_count=count
    )


def _feasible_days(job: Job, horizon: Sequence[date], tz: tzinfo) -> list[date]:
    """Days this job could legitimately be served on.

    Combines the customer's window with material lead time. A job with no window may
    go anywhere from the moment its parts exist.
    """
    ready_on = job.earliest_material_date(job.requested_at.date())
    days: list[date] = []
    for on_date in horizon:
        if on_date < ready_on:
            continue
        if not job.windows:
            days.append(on_date)
            continue
        begins = _day_start(on_date, tz)
        ends = begins + timedelta(days=1)
        if any(w.start < ends and begins < w.end for w in job.windows):
            days.append(on_date)
    return days


def _certified_days(job: Job, horizon: Sequence[date], world: WorldState, tz: tzinfo) -> set[date]:
    """Days on which enough certified people are actually working."""
    out: set[date] = set()
    for on_date in horizon:
        day_begins = _day_start(on_date, tz)
        available: list[Worker] = []
        for worker in world.workers.values():
            span = _shift_window(worker, on_date)
            if span is None:
                continue
            if _largest_free_interval(
                world.worker_outages.get(worker.id, ()), day_begins, span[0], span[1]
            ):
                available.append(worker)
        certified = [w for w in available if job.required_certifications <= w.certifications]
        if len(certified) >= job.crew_size:
            out.add(on_date)
    return out


# ------------------------------------------------------------------------- stage A


def assign_days(
    *,
    world: WorldState,
    jobs: Sequence[Job],
    horizon: Sequence[date],
    params: SolveParams,
    horizon_params: HorizonParams,
    capacity_override: dict[date, int] | None = None,
) -> tuple[dict[JobId, date], list[UnservedJob]]:
    """Choose a day for each job. Returns the assignment plus jobs placed nowhere.

    ``capacity_override`` lets the caller tighten a day after Stage B has shown what
    that day can genuinely absorb - this is the feedback edge of the two-stage loop.
    """
    tz = params.business_tz
    depot = next(iter(sorted(world.vans.values(), key=lambda v: v.id)), None)
    depot_location = depot.home_depot if depot else Location(lat=0.0, lon=0.0)

    capacities = {
        d: day_capacity(world, d, tz, horizon_params.capacity_utilization) for d in horizon
    }
    if capacity_override:
        capacities = {
            d: DayCapacity(
                on_date=d,
                total_person_minutes=min(c.total_person_minutes, capacity_override.get(d, 10**9)),
                by_certification={
                    k: min(v, capacity_override.get(d, 10**9))
                    for k, v in c.by_certification.items()
                },
                worker_count=c.worker_count,
            )
            for d, c in capacities.items()
        }

    model = cp_model.CpModel()
    allowed: dict[JobId, list[date]] = {}
    unplaceable: list[UnservedJob] = []

    for job in jobs:
        windows_ok = _feasible_days(job, horizon, tz)
        staffed = _certified_days(job, horizon, world, tz)
        options = [d for d in windows_ok if d in staffed]
        if not options:
            unplaceable.append(_why_no_day(job, horizon, windows_ok, staffed, tz))
            continue
        allowed[job.id] = options

    placeable = [j for j in jobs if j.id in allowed]
    if not placeable:
        return {}, unplaceable

    on_day = {
        (job.id, d): model.new_bool_var(f"day_{job.id}_{d}")
        for job in placeable
        for d in allowed[job.id]
    }
    placed = {job.id: model.new_bool_var(f"placed_{job.id}") for job in placeable}

    for job in placeable:
        model.add(sum(on_day[job.id, d] for d in allowed[job.id]) == placed[job.id])

    # Capacity: overall, and per certification so one scarce skill cannot be
    # silently oversubscribed by a day that looks roomy in aggregate.
    for on_date in horizon:
        capacity = capacities[on_date]
        demand = [
            on_day[job.id, on_date] * (job.estimated_duration_min * job.crew_size)
            for job in placeable
            if on_date in allowed[job.id]
        ]
        if demand:
            model.add(sum(demand) <= capacity.total_person_minutes)

        for cert in Certification:
            cert_demand = [
                on_day[job.id, on_date] * (job.estimated_duration_min * job.crew_size)
                for job in placeable
                if on_date in allowed[job.id] and cert in job.required_certifications
            ]
            if cert_demand:
                model.add(sum(cert_demand) <= capacity.by_certification.get(cert, 0))

    # Geographic cohesion: charge each day for every sector it touches.
    sector_used = {
        (on_date, s): model.new_bool_var(f"sector_{on_date}_{s}")
        for on_date in horizon
        for s in range(SECTORS)
    }
    for job in placeable:
        sector = _sector_of(job.location, depot_location)
        for on_date in allowed[job.id]:
            model.add_implication(on_day[job.id, on_date], sector_used[on_date, sector])

    terms: list[cp_model.LinearExpr] = []
    for job in placeable:
        terms.append((1 - placed[job.id]) * _cents(_unserved_penalty(job, params)))
        urgency = {"emergency": 6.0, "high": 2.5, "normal": 1.0, "low": 0.5}[job.priority.value]
        for index, on_date in enumerate(horizon):
            if on_date in allowed[job.id]:
                delay = _cents(horizon_params.day_delay_penalty * urgency * index)
                if delay:
                    terms.append(on_day[job.id, on_date] * delay)

    spread = _cents(horizon_params.sector_spread_penalty)
    for key in sector_used:
        terms.append(sector_used[key] * spread)

    model.minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = params.max_solve_seconds
    solver.parameters.num_search_workers = params.search_workers
    solver.parameters.random_seed = params.random_seed
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {}, unplaceable + [
            UnservedJob(
                job_id=job.id,
                reason=UnservedReason.NO_CAPACITY_IN_HORIZON,
                detail="day assignment found no feasible allocation across the horizon",
            )
            for job in placeable
        ]

    assignment: dict[JobId, date] = {}
    for job in placeable:
        if not solver.boolean_value(placed[job.id]):
            unplaceable.append(
                UnservedJob(
                    job_id=job.id,
                    reason=UnservedReason.NO_CAPACITY_IN_HORIZON,
                    detail=(
                        "no day in the horizon had enough crew-hours for this job "
                        "at an acceptable cost"
                    ),
                )
            )
            continue
        for on_date in allowed[job.id]:
            if solver.boolean_value(on_day[job.id, on_date]):
                assignment[job.id] = on_date
                break
    return assignment, unplaceable


def _why_no_day(
    job: Job,
    horizon: Sequence[date],
    windows_ok: Sequence[date],
    staffed: set[date],
    tz: tzinfo,
) -> UnservedJob:
    """Distinguish "not in this horizon" from "nobody can do it" from "parts missing"."""
    ready_on = job.earliest_material_date(job.requested_at.date())
    if ready_on > horizon[-1]:
        pending = [m.part_code for m in job.materials if not m.in_stock]
        return UnservedJob(
            job_id=job.id,
            reason=UnservedReason.MATERIALS_NOT_AVAILABLE,
            detail=(
                f"{pending or ['materials']} not available until {ready_on.isoformat()}, "
                f"past the horizon ending {horizon[-1].isoformat()}"
            ),
        )
    if not windows_ok:
        when = ", ".join(w.start.astimezone(tz).date().isoformat() for w in job.windows)
        return UnservedJob(
            job_id=job.id,
            reason=UnservedReason.WINDOW_ON_ANOTHER_DAY,
            detail=(
                f"customer window falls on {when}, outside the horizon "
                f"{horizon[0].isoformat()}..{horizon[-1].isoformat()}"
            ),
        )
    if not staffed:
        missing = sorted(c.value for c in job.required_certifications)
        return UnservedJob(
            job_id=job.id,
            reason=UnservedReason.NO_CERTIFIED_WORKER,
            detail=(
                f"no day in the horizon has {job.crew_size} available worker(s) "
                f"holding {missing or 'the required skills'}"
            ),
        )
    return UnservedJob(
        job_id=job.id,
        reason=UnservedReason.NO_CAPACITY_IN_HORIZON,
        detail="the days this job could run on have nobody certified available",
    )


# ------------------------------------------------------------- stage A + B together


def plan_horizon(
    *,
    world: WorldState,
    travel: TravelOracle,
    start: date,
    params: SolveParams,
    horizon_params: HorizonParams,
    candidate_job_ids: Sequence[JobId] | None = None,
    locked_job_ids: Sequence[JobId] = (),
    pinned_days: Mapping[JobId, date] | None = None,
) -> HorizonResult:
    """Plan a rolling horizon: assign days, route each day, then correct and repeat."""
    horizon = _dates(start, horizon_params.days)
    if candidate_job_ids is not None:
        candidates = [world.jobs[j] for j in candidate_job_ids if j in world.jobs]
    elif locked_job_ids:
        # Repair mode: work already in flight must be carried into the new plan, not
        # quietly dropped because it is no longer "schedulable".
        candidates = world.active_jobs()
    else:
        candidates = world.schedulable_jobs()
    locked = set(locked_job_ids)

    pinned = dict(pinned_days or {})
    overrides: dict[date, int] = {}
    assignment: dict[JobId, date] = {}
    unplaceable: list[UnservedJob] = []
    day_results: dict[date, DayPlanResult] = {}
    rounds = 0

    for round_index in range(horizon_params.max_rounds):
        rounds = round_index + 1
        assignment, unplaceable = assign_days(
            world=world,
            jobs=[j for j in candidates if j.id not in locked],
            horizon=horizon,
            params=params,
            horizon_params=horizon_params,
            capacity_override=overrides or None,
        )
        # Locked work keeps the day it is already on; day assignment has no say.
        for job_id in locked:
            placement = pinned.get(job_id)
            if placement is not None:
                assignment[job_id] = placement

        day_results = {}
        spilled: list[JobId] = []
        for on_date in horizon:
            todays = [job_id for job_id, d in assignment.items() if d == on_date]
            if not todays:
                continue
            result = plan_day(
                world=world,
                travel=travel,
                on_date=on_date,
                candidate_job_ids=todays,
                params=params,
                locked_job_ids=[j for j in todays if j in locked],
            )
            day_results[on_date] = result
            scheduled = {job_id for route in result.routes for job_id in route.job_ids}
            missed = [j for j in todays if j not in scheduled]
            if missed:
                spilled.extend(missed)
                # Tighten this day to what it actually absorbed, so the next round
                # sends the overflow somewhere with genuine room.
                absorbed = sum(
                    world.jobs[j].estimated_duration_min * world.jobs[j].crew_size
                    for j in scheduled
                )
                overrides[on_date] = max(absorbed, 0)

        if not spilled:
            break

    routes = tuple(route for result in day_results.values() for route in result.routes)
    scheduled_ids = {job_id for route in routes for job_id in route.job_ids}

    unserved: list[UnservedJob] = list(unplaceable)
    seen = {u.job_id for u in unserved}
    for result in day_results.values():
        for item in result.unserved:
            if item.job_id not in scheduled_ids and item.job_id not in seen:
                unserved.append(item)
                seen.add(item.job_id)
    for job in candidates:
        if job.id not in scheduled_ids and job.id not in seen:
            unserved.append(
                UnservedJob(
                    job_id=job.id,
                    reason=UnservedReason.NO_CAPACITY_IN_HORIZON,
                    detail="assigned to a day that could not route it, and nowhere else fit",
                )
            )
            seen.add(job.id)

    return HorizonResult(
        routes=routes,
        unserved=tuple(unserved),
        released_promises=tuple(
            sorted({j for r in day_results.values() for j in r.released_promises})
        ),
        day_results=day_results,
        assignment=assignment,
        rounds=rounds,
        metrics={
            "candidates": float(len(candidates)),
            "scheduled": float(len(scheduled_ids)),
            "days_used": float(len(day_results)),
            "rounds": float(rounds),
            "solve_seconds": sum(r.metrics.get("solve_seconds", 0.0) for r in day_results.values()),
        },
    )
