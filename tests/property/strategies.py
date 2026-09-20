"""Generating small, plausible worlds.

Example-based tests check the cases someone thought of. These check the ones nobody
did - a worker whose shift ends before a job's window opens, two jobs in the same
building, a van with no stock of anything. Those are the shapes that break assumptions
quietly, and writing them out by hand means only ever writing out the ones already
imagined.

Worlds are kept deliberately small. A solve is tens of milliseconds, and a hundred
examples of a three-crew day finds more than five examples of a realistic one - the
interesting failures are structural, not a matter of scale.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from hypothesis import strategies as st

from glass_guru.domain.enums import Certification, Priority, ServiceType, WindowHardness
from glass_guru.domain.models import (
    DayHours,
    Job,
    Location,
    Material,
    TimeWindow,
    Van,
    Worker,
)
from glass_guru.domain.state import WorldState

TZ = timezone(timedelta(hours=-7))
DAY = date(2026, 9, 21)

#: A few square miles around one depot. Random points over a continent would make
#: every job unreachable and every generated world trivially infeasible.
LAT = st.floats(min_value=47.50, max_value=47.72, allow_nan=False, allow_infinity=False)
LON = st.floats(min_value=-122.42, max_value=-122.20, allow_nan=False, allow_infinity=False)

DEPOT = Location(lat=47.5701, lon=-122.3334, address="depot")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=TZ)


locations = st.builds(Location, lat=LAT, lon=LON)
certifications = st.sampled_from(list(Certification))


@st.composite
def shifts(draw: st.DrawFn) -> tuple[DayHours, ...]:
    """A shift that always contains at least four hours of working day."""
    start_hour = draw(st.integers(min_value=6, max_value=10))
    length = draw(st.integers(min_value=4, max_value=10))
    end_hour = min(23, start_hour + length)
    return tuple(DayHours(weekday=d, start=time(start_hour), end=time(end_hour)) for d in range(7))


@st.composite
def workers(draw: st.DrawFn, index: int = 0) -> Worker:
    return Worker(
        id=f"w-{index}",
        name=f"Worker {index}",
        certifications=frozenset(draw(st.sets(certifications, min_size=1, max_size=3))),
        working_hours=draw(shifts()),
        home_location=draw(locations),
        overtime_eligible=draw(st.booleans()),
        loaded_cost_per_hour=draw(st.floats(min_value=30.0, max_value=90.0)),
    )


@st.composite
def vans(draw: st.DrawFn, index: int = 0) -> Van:
    parts = draw(
        st.dictionaries(
            st.sampled_from(["annealed_std", "tempered_std", "screen_kit", "shower_kit"]),
            st.integers(min_value=0, max_value=6),
            max_size=4,
        )
    )
    return Van(
        id=f"van-{index}",
        label=f"Van {index}",
        rack_slots=draw(st.integers(min_value=2, max_value=12)),
        stock=parts,
        home_depot=DEPOT,
    )


@st.composite
def jobs(draw: st.DrawFn, index: int = 0) -> Job:
    """A job whose window is wide enough to be worth attempting.

    Degenerate jobs - a three-hour install inside a one-hour window - are worth
    testing, but generating mostly those would mean testing the unserved path over and
    over rather than the routing that follows from a placeable one.
    """
    duration = draw(st.integers(min_value=30, max_value=180))
    window_start = draw(st.integers(min_value=6, max_value=13))
    window_length = draw(st.integers(min_value=2, max_value=10))
    window_end = min(23, window_start + window_length)

    return Job(
        id=f"j-{index}",
        customer_id=f"c-{index}",
        customer_name=f"Customer {index}",
        location=draw(locations),
        service_type=draw(st.sampled_from(list(ServiceType))),
        required_certifications=frozenset(draw(st.sets(certifications, max_size=2))),
        crew_size=draw(st.integers(min_value=1, max_value=2)),
        estimated_duration_min=duration,
        materials=tuple(
            Material(part_code=part, quantity=1, in_stock=True)
            for part in draw(st.sets(st.sampled_from(["annealed_std", "screen_kit"]), max_size=1))
        ),
        windows=(
            TimeWindow(
                start=at(window_start),
                end=at(window_end),
                hardness=draw(st.sampled_from([WindowHardness.SOFT, WindowHardness.HARD])),
            ),
        ),
        priority=draw(st.sampled_from(list(Priority))),
        revenue=draw(st.floats(min_value=0.0, max_value=3000.0)),
        requested_at=at(5),
    )


@st.composite
def worlds(
    draw: st.DrawFn,
    min_workers: int = 1,
    max_workers: int = 4,
    min_jobs: int = 1,
    max_jobs: int = 6,
) -> WorldState:
    worker_count = draw(st.integers(min_value=min_workers, max_value=max_workers))
    van_count = draw(st.integers(min_value=1, max_value=3))
    job_count = draw(st.integers(min_value=min_jobs, max_value=max_jobs))

    roster = [draw(workers(index=i)) for i in range(worker_count)]
    fleet = [draw(vans(index=i)) for i in range(van_count)]
    work = [draw(jobs(index=i)) for i in range(job_count)]

    return WorldState(
        as_of=at(5),
        workers={w.id: w for w in roster},
        vans={v.id: v for v in fleet},
        jobs={j.id: j for j in work},
    )
