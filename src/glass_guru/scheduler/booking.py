"""What does it cost us to serve this job, and when is it cheapest?

This is the highest-value thing the deterministic engine does, and the cleanest
illustration of the split the whole project rests on. When a call comes in, the
useful question is not "what is the optimal schedule" - it is "where does this job
fit, and what does each option cost?" The agent runs the conversation; the solver
computes the money.

The method is marginal-cost insertion. Hold the committed plan fixed, try the job in
each feasible day, and price the difference. Concretely, for a 90-minute residential
job out in one direction:

    Tue 14:00   already two stops on that street that afternoon, +18 min detour   $22
    Wed 09:00   moderate detour                                                   $41
    Thu 08:00   a dedicated trip out and back                                     $82

A dispatcher can then say "I can do Tuesday afternoon or Thursday morning" and know
that Tuesday is not a preference, it is four times cheaper to serve. Over a year that
is the difference between 22 and 28 jobs a week on the same headcount.

Two things make the number trustworthy. Existing work is *locked*, so a quote can
never be cheap because it silently displaced a customer who was already promised a
slot. And the delta is computed from materialized routes rather than the solver's
objective, so it is real dollars of driving and labour rather than a score that
includes penalty terms for the job not yet existing.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import UnservedReason
from glass_guru.domain.models import Job, JobId, TimeWindow
from glass_guru.domain.state import WorldState
from glass_guru.domain.travel import TravelOracle
from glass_guru.scheduler.costing import RouteCost, cost_route
from glass_guru.scheduler.day_planner import SolveParams, plan_day


@dataclass(frozen=True, slots=True)
class SlotSuggestion:
    """One bookable option, priced and explained."""

    on_date: date
    arrival: datetime
    quoted_window: TimeWindow
    marginal_cost: float
    crew_id: str
    worker_names: tuple[str, ...]
    added_travel_minutes: int
    added_travel_miles: float
    reason: str

    def describe(self, tz: tzinfo) -> str:
        start = self.quoted_window.start.astimezone(tz)
        end = self.quoted_window.end.astimezone(tz)
        return (
            f"{start:%a %d %b} {start:%H:%M}-{end:%H:%M}  ${self.marginal_cost:,.2f}  {self.reason}"
        )


@dataclass(frozen=True, slots=True)
class UnavailableDay:
    """A day the job cannot be served on, with the reason a dispatcher can act on."""

    on_date: date
    reason: UnservedReason
    detail: str


@dataclass(frozen=True, slots=True)
class BookingOptions:
    slots: tuple[SlotSuggestion, ...]
    unavailable: tuple[UnavailableDay, ...]
    evaluated_days: int

    @property
    def best(self) -> SlotSuggestion | None:
        return self.slots[0] if self.slots else None

    @property
    def savings_vs_worst(self) -> float:
        """What choosing well is worth. The number that makes this feature pay."""
        if len(self.slots) < 2:
            return 0.0
        return self.slots[-1].marginal_cost - self.slots[0].marginal_cost


@dataclass(frozen=True, slots=True)
class BaselineDay:
    """What the day costs before the new job is inserted."""

    operating_cost: float
    served: frozenset[JobId]
    travel_minutes: int
    travel_miles: float


@dataclass
class BaselineCache:
    """The cost of a day as it already stands, remembered between quotes.

    Half of a quote is re-solving the untouched day, and that answer does not change
    between one caller and the next: the plan only moves when something is committed.
    Caching it halves the wait, which at the real operating point is the difference
    between three seconds and a second and a half - and three seconds is where a
    dispatcher starts apologising for the pause.

    Keyed on the day and the exact set of jobs in it, so a booking, a cancellation or
    a disruption invalidates it by construction rather than by remembering to.
    """

    _entries: dict[tuple[date, frozenset[JobId], float], BaselineDay] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, on_date: date, jobs: Sequence[JobId], budget: float) -> BaselineDay | None:
        found = self._entries.get((on_date, frozenset(jobs), budget))
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, on_date: date, jobs: Sequence[JobId], budget: float, day: BaselineDay) -> None:
        self._entries[(on_date, frozenset(jobs), budget)] = day


def _world_with(world: WorldState, job: Job) -> WorldState:
    """A shallow copy of the world with one extra job. The original is untouched."""
    clone = copy(world)
    clone.jobs = {**world.jobs, job.id: job}
    return clone


def _route_operating_cost(route_cost: RouteCost) -> float:
    """Driving and labour only - the marginal cost of *serving*, not of penalties."""
    return float(route_cost.travel_labor + route_cost.vehicle + route_cost.overtime)


def _reason_for(
    added_minutes: int,
    added_miles: float,
    neighbours: int,
    dedicated: bool,
) -> str:
    """Explain the price in the terms a dispatcher would use on the phone."""
    if dedicated:
        return f"a dedicated trip out and back, +{added_minutes} min driving"
    if neighbours and added_minutes == 0:
        # Same ~150m cell as work already booked: below the travel cache's resolution,
        # so the detour genuinely is free. Saying "+0 min" reads like a bug.
        return f"on the same block as {neighbours} stop(s) already booked that day"
    if neighbours and added_minutes <= 20:
        near = "stop" if neighbours == 1 else "stops"
        return f"already {neighbours} {near} nearby that day, +{added_minutes} min detour"
    if neighbours:
        return f"{neighbours} other stop(s) that day, +{added_minutes} min detour"
    return f"+{added_minutes} min / {added_miles:.1f} mi added to the day"


def suggest_booking_slots(
    *,
    world: WorldState,
    travel: TravelOracle,
    draft: Job,
    horizon: list[date],
    params: SolveParams,
    business: BusinessParams,
    limit: int = 5,
    cache: BaselineCache | None = None,
) -> BookingOptions:
    """Rank the days this job could be served on by what serving it actually costs."""
    tz = params.business_tz
    quoted_minutes = int(business.scheduling.quoted_window_minutes.value)
    candidate_world = _world_with(world, draft)

    # A quote is two solves per day with a caller waiting, so it gets its own ceiling.
    # Inheriting the batch budget made a twenty-five job day take twenty seconds to
    # answer, which is not a feature anybody would use.
    #
    # Unless reproducibility was asked for. Clamping the wall clock below a
    # deterministic budget makes the clock the binding limit again and quietly undoes
    # it - a caller who wants the same answer every time has accepted the longer wait.
    if params.max_deterministic_time is None:
        params = replace(params, max_solve_seconds=business.solver.quote_solve_seconds.value)

    slots: list[SlotSuggestion] = []
    unavailable: list[UnavailableDay] = []

    for on_date in horizon:
        existing = [
            job.id
            for job in world.schedulable_jobs()
            if any(
                w.start.astimezone(tz).date() == on_date
                or (w.start.date() <= on_date <= w.end.date())
                for w in job.windows
            )
        ]

        remembered = cache.get(on_date, existing, params.max_solve_seconds) if cache else None
        if remembered is None:
            baseline = plan_day(
                world=world,
                travel=travel,
                on_date=on_date,
                candidate_job_ids=existing,
                params=params,
            )
            remembered = BaselineDay(
                operating_cost=sum(
                    _route_operating_cost(cost_route(route, world, business, tz))
                    for route in baseline.routes
                ),
                served=frozenset(j for route in baseline.routes for j in route.job_ids),
                travel_minutes=sum(r.total_travel_minutes for r in baseline.routes),
                travel_miles=sum(r.total_travel_miles for r in baseline.routes),
            )
            if cache is not None:
                cache.put(on_date, existing, params.max_solve_seconds, remembered)

        # Everything already placed stays placed. A quote must never look cheap
        # because it quietly displaced someone who was already promised a slot.
        trial = plan_day(
            world=candidate_world,
            travel=travel,
            on_date=on_date,
            candidate_job_ids=[*existing, draft.id],
            params=params,
            locked_job_ids=sorted(remembered.served),
        )

        placement = next(
            (
                (route, stop)
                for route in trial.routes
                for stop in route.stops
                if stop.job_id == draft.id
            ),
            None,
        )
        if placement is None:
            miss = next((u for u in trial.unserved if u.job_id == draft.id), None)
            unavailable.append(
                UnavailableDay(
                    on_date=on_date,
                    reason=miss.reason if miss else UnservedReason.NO_CAPACITY_IN_HORIZON,
                    detail=miss.detail if miss else "no room on this day",
                )
            )
            continue

        route, stop = placement
        trial_cost = sum(
            _route_operating_cost(cost_route(r, candidate_world, business, tz))
            for r in trial.routes
        )
        # Travel deltas come from the trial alone: the baseline's route objects are
        # not kept, only its cost, which is all the marginal figure needs.
        added_minutes = max(
            0, sum(r.total_travel_minutes for r in trial.routes) - remembered.travel_minutes
        )
        added_miles = max(
            0.0, sum(r.total_travel_miles for r in trial.routes) - remembered.travel_miles
        )

        neighbours = len(route.stops) - 1
        dedicated = neighbours == 0
        half = timedelta(minutes=quoted_minutes / 2)
        window = TimeWindow(start=stop.arrival - half, end=stop.arrival + half)

        slots.append(
            SlotSuggestion(
                on_date=on_date,
                arrival=stop.arrival,
                quoted_window=window,
                marginal_cost=max(0.0, trial_cost - remembered.operating_cost),
                crew_id=route.crew_id,
                worker_names=tuple(
                    world.workers[w].name for w in route.worker_ids if w in world.workers
                ),
                added_travel_minutes=added_minutes,
                added_travel_miles=added_miles,
                reason=_reason_for(added_minutes, added_miles, neighbours, dedicated),
            )
        )

    slots.sort(key=lambda s: (s.marginal_cost, s.on_date))
    return BookingOptions(
        slots=tuple(slots[:limit]),
        unavailable=tuple(unavailable),
        evaluated_days=len(horizon),
    )
