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

from copy import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import UnservedReason
from glass_guru.domain.models import Job, TimeWindow
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
) -> BookingOptions:
    """Rank the days this job could be served on by what serving it actually costs."""
    tz = params.business_tz
    quoted_minutes = int(business.scheduling.quoted_window_minutes.value)
    candidate_world = _world_with(world, draft)

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

        baseline = plan_day(
            world=world,
            travel=travel,
            on_date=on_date,
            candidate_job_ids=existing,
            params=params,
        )
        baseline_cost = sum(
            _route_operating_cost(cost_route(route, world, business, tz))
            for route in baseline.routes
        )
        baseline_served = {j for route in baseline.routes for j in route.job_ids}

        # Everything already placed stays placed. A quote must never look cheap
        # because it quietly displaced someone who was already promised a slot.
        trial = plan_day(
            world=candidate_world,
            travel=travel,
            on_date=on_date,
            candidate_job_ids=[*existing, draft.id],
            params=params,
            locked_job_ids=sorted(baseline_served),
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
        added_minutes = sum(r.total_travel_minutes for r in trial.routes) - sum(
            r.total_travel_minutes for r in baseline.routes
        )
        added_miles = sum(r.total_travel_miles for r in trial.routes) - sum(
            r.total_travel_miles for r in baseline.routes
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
                marginal_cost=max(0.0, trial_cost - baseline_cost),
                crew_id=route.crew_id,
                worker_names=tuple(
                    world.workers[w].name for w in route.worker_ids if w in world.workers
                ),
                added_travel_minutes=max(0, added_minutes),
                added_travel_miles=max(0.0, added_miles),
                reason=_reason_for(
                    max(0, added_minutes), max(0.0, added_miles), neighbours, dedicated
                ),
            )
        )

    slots.sort(key=lambda s: (s.marginal_cost, s.on_date))
    return BookingOptions(
        slots=tuple(slots[:limit]),
        unavailable=tuple(unavailable),
        evaluated_days=len(horizon),
    )
