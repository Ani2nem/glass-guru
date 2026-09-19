"""Turning a *sequence* of jobs into a concrete route with real clock times.

The solver decides which jobs a crew does and in what order. This module decides
what that costs in wall-clock terms: when the van leaves, how long each drive takes
at the time of day it actually happens, and when the crew gets home.

Keeping it separate matters because the solver optimizes over an approximation
(integer minutes, a snapshot matrix) while this replays the sequence against the
travel oracle at the real departure time of every leg. Anything the approximation
got wrong shows up here as a later arrival, and then the invariant checker catches
it. Solver, materializer, and checker are three independent views of the same plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from glass_guru.domain.enums import WindowHardness
from glass_guru.domain.models import (
    CrewRoute,
    Job,
    JobId,
    Stop,
    VanId,
    WorkerId,
)
from glass_guru.domain.state import WorldState
from glass_guru.domain.travel import TravelOracle


def _earliest_start(job: Job, arrival: datetime) -> datetime:
    """When service can actually begin, accounting for waiting at a closed door.

    Picks the first declared window the crew can still satisfy after arriving; if
    they are early, they wait. Returns the raw arrival when no window fits, so the
    infeasibility surfaces in the invariant checker rather than being silently
    papered over here.
    """
    duration = timedelta(minutes=job.estimated_duration_min)
    best: datetime | None = None
    for window in job.windows:
        start = max(arrival, window.start)
        fits = window.hardness is WindowHardness.SOFT or start + duration <= window.end
        if fits and (best is None or start < best):
            best = start
    return best if best is not None else arrival


def materialize_route(
    *,
    world: WorldState,
    travel: TravelOracle,
    crew_id: str,
    worker_ids: Sequence[WorkerId],
    van_id: VanId,
    on_date: date,
    job_sequence: Sequence[JobId],
    shift_start: datetime,
) -> CrewRoute:
    """Replay a job sequence against the travel oracle to produce concrete stops.

    ``shift_start`` is when the crew leaves the depot. Every leg is priced at its own
    departure time, so a route that crosses into rush hour pays rush-hour minutes.
    """
    van = world.vans[van_id]
    position = van.home_depot
    clock = shift_start
    stops: list[Stop] = []

    for job_id in job_sequence:
        job = world.jobs[job_id]
        leg = travel.leg(position, job.location, clock)
        arrival = _earliest_start(job, clock + timedelta(minutes=leg.minutes))
        departure = arrival + timedelta(minutes=job.estimated_duration_min)
        stops.append(
            Stop(
                job_id=job_id,
                arrival=arrival,
                departure=departure,
                travel_minutes_from_prev=leg.minutes,
                travel_miles_from_prev=leg.miles,
            )
        )
        position = job.location
        clock = departure

    home = travel.leg(position, van.home_depot, clock) if stops else None
    return CrewRoute(
        crew_id=crew_id,
        date=on_date,
        worker_ids=tuple(worker_ids),
        van_id=van_id,
        stops=tuple(stops),
        return_to_depot_minutes=home.minutes if home else 0,
        return_to_depot_miles=home.miles if home else 0.0,
    )
