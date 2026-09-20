"""Where does this fall over?

The business does about 25 jobs a day. The interesting question is not whether the
solver handles that - it does, in tens of milliseconds - but what happens at four
times the volume, because that is the difference between "works today" and "survives
the year it grows".

Two numbers matter and they fail differently. **Solve time** degrades gracefully: a
slow morning plan is annoying. **Hot-path latency** does not: a booking quote computed
while a customer is on the phone has a few seconds before the dispatcher starts
apologising, and past that the feature is not worth having.

So this reports them separately, and reports the *gap* a quote is chosen from as well
as the time it took - a fast answer that is always the same day is a fast useless
answer.

Synthetic travel throughout. This measures how the solver scales with problem size,
not how accurate the roads are, and the frozen snapshot only covers fixture addresses.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from glass_guru.config import BusinessParams
from glass_guru.domain.catalog import CATALOG
from glass_guru.domain.enums import Certification, Priority, ServiceType, WindowHardness
from glass_guru.domain.models import (
    Job,
    Location,
    Material,
    TimeWindow,
    Van,
    Worker,
)
from glass_guru.domain.state import WorldState
from glass_guru.fixtures.sample_business import DEPOT, VANS, WEEK_START, WORKERS, _at
from glass_guru.scheduler.booking import suggest_booking_slots
from glass_guru.scheduler.day_planner import SolveParams, plan_day
from glass_guru.scheduler.horizon import HorizonParams, plan_horizon
from glass_guru.scheduler.travel.base import TravelProvider
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider

#: Roughly the real service area: a 30-mile radius around the depot.
LAT_SPREAD = 0.18
LON_SPREAD = 0.22


@dataclass(frozen=True, slots=True)
class LoadResult:
    label: str
    jobs: int
    workers: int
    vans: int
    solve_seconds: float
    jobs_served: int
    status: str
    quote_seconds: float = 0.0
    quote_options: int = 0
    quote_spread: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def served_fraction(self) -> float:
        return self.jobs_served / self.jobs if self.jobs else 1.0


def synthetic_world(
    *,
    jobs: int,
    workers: int,
    vans: int,
    seed: int = 0,
    on_date: date = WEEK_START,
) -> WorldState:
    """A business of arbitrary size, shaped like the real one.

    Jobs are drawn from the catalogue rather than invented, so the certification mix,
    crew sizes and durations stay plausible: a load test against work nobody is
    qualified for measures the unserved path, not the routing.
    """
    rng = random.Random(seed)
    services = list(CATALOG.values())

    roster: dict[str, Worker] = {}
    for index in range(workers):
        person = WORKERS[index % len(WORKERS)]
        roster[f"w-{index}"] = person.model_copy(
            update={"id": f"w-{index}", "name": f"{person.name} {index}"}
        )

    fleet: dict[str, Van] = {}
    for index in range(vans):
        vehicle = VANS[index % len(VANS)]
        fleet[f"van-{index}"] = vehicle.model_copy(
            update={
                "id": f"van-{index}",
                # Generous stock: this measures routing, not procurement.
                "stock": dict.fromkeys(vehicle.stock, 99),
                "rack_slots": 99,
            }
        )

    work: dict[str, Job] = {}
    for index in range(jobs):
        entry = services[rng.randrange(len(services))]
        opens = rng.randrange(7, 14)
        width = rng.randrange(3, 9)
        work[f"j-{index}"] = Job(
            id=f"j-{index}",
            customer_id=f"c-{index}",
            customer_name=f"Customer {index}",
            location=Location(
                lat=DEPOT.lat + rng.uniform(-LAT_SPREAD, LAT_SPREAD) / 2,
                lon=DEPOT.lon + rng.uniform(-LON_SPREAD, LON_SPREAD) / 2,
            ),
            service_type=entry.service_type,
            required_certifications=entry.required_certifications,
            crew_size=entry.crew_size,
            estimated_duration_min=entry.typical_duration_min,
            materials=tuple(
                Material(part_code=part, quantity=1, in_stock=True) for part in entry.typical_parts
            ),
            windows=(
                TimeWindow(
                    start=_at(0, opens),
                    end=_at(0, min(20, opens + width)),
                    hardness=(WindowHardness.HARD if rng.random() < 0.15 else WindowHardness.SOFT),
                ),
            ),
            priority=Priority.NORMAL,
            revenue=float(rng.randrange(150, 2800)),
            requested_at=_at(-4, 9),
        )

    return WorldState(as_of=_at(0, 6), workers=roster, vans=fleet, jobs=work)


def _quote(
    world: WorldState,
    travel: TravelProvider,
    params: SolveParams,
    business: BusinessParams,
) -> tuple[float, int, float]:
    """Time a booking quote: the hot path, with a customer waiting."""
    entry = CATALOG[ServiceType.SCREEN_REPAIR]
    draft = Job(
        id="load-draft",
        customer_id="c-draft",
        customer_name="Caller",
        location=Location(lat=DEPOT.lat + 0.03, lon=DEPOT.lon - 0.04),
        service_type=entry.service_type,
        required_certifications=frozenset({Certification.SCREEN_REPAIR}),
        estimated_duration_min=entry.typical_duration_min,
        windows=(TimeWindow(start=_at(0, 8), end=_at(0, 17)),),
        requested_at=world.as_of,
    )
    started = time.monotonic()
    options = suggest_booking_slots(
        world=world,
        travel=travel,
        draft=draft,
        horizon=[WEEK_START],
        params=params,
        business=business,
    )
    return time.monotonic() - started, len(options.slots), options.savings_vs_worst


def run_day(
    *, jobs: int, workers: int, vans: int, seed: int = 0, budget_seconds: float = 5.0
) -> LoadResult:
    business = BusinessParams.load()
    tz = ZoneInfo(business.meta.timezone)
    travel = SyntheticTravelProvider.from_business(business)
    params = SolveParams.from_business(business, tz, max_solve_seconds=budget_seconds)

    world = synthetic_world(jobs=jobs, workers=workers, vans=vans, seed=seed)

    started = time.monotonic()
    result = plan_day(
        world=world,
        travel=travel,
        on_date=WEEK_START,
        candidate_job_ids=list(world.jobs),
        params=params,
    )
    elapsed = time.monotonic() - started
    served = len({job_id for route in result.routes for job_id in route.job_ids})

    quote_seconds, quote_options, quote_spread = _quote(world, travel, params, business)

    notes: list[str] = []
    if result.status != "OPTIMAL":
        notes.append(f"stopped at {result.status} after {budget_seconds:.0f}s")
    if quote_seconds > 3.0:
        notes.append("a quote at this size is too slow to give on a call")

    return LoadResult(
        label=f"{jobs} jobs / {workers} workers / {vans} vans",
        jobs=jobs,
        workers=workers,
        vans=vans,
        solve_seconds=elapsed,
        jobs_served=served,
        status=result.status,
        quote_seconds=quote_seconds,
        quote_options=quote_options,
        quote_spread=quote_spread,
        notes=tuple(notes),
    )


def run_horizon(*, jobs: int, workers: int, vans: int, seed: int = 0) -> LoadResult:
    business = BusinessParams.load()
    tz = ZoneInfo(business.meta.timezone)
    travel = SyntheticTravelProvider.from_business(business)
    params = SolveParams.from_business(business, tz)
    horizon_params = HorizonParams.from_business(business)

    world = synthetic_world(jobs=jobs, workers=workers, vans=vans, seed=seed)
    # Spread windows across the horizon so day assignment has something to decide.
    spread: dict[str, Job] = {}
    for index, (job_id, job) in enumerate(world.jobs.items()):
        day = index % horizon_params.days
        window = job.windows[0]
        spread[job_id] = job.model_copy(
            update={
                "windows": (
                    TimeWindow(
                        start=window.start + timedelta(days=day),
                        end=window.end + timedelta(days=day),
                        hardness=window.hardness,
                    ),
                )
            }
        )
    world.jobs = spread

    started = time.monotonic()
    result = plan_horizon(
        world=world,
        travel=travel,
        start=WEEK_START,
        params=params,
        horizon_params=horizon_params,
    )
    elapsed = time.monotonic() - started

    return LoadResult(
        label=f"{jobs} jobs over {horizon_params.days} days",
        jobs=jobs,
        workers=workers,
        vans=vans,
        solve_seconds=elapsed,
        jobs_served=len(result.scheduled_job_ids),
        status="OPTIMAL",
        notes=(f"{result.rounds} assignment round(s)",),
    )


def sweep(sizes: Sequence[int] = (25, 50, 75, 100), *, scale_crew: bool = True) -> list[LoadResult]:
    """Volume against a roster that grows with it, or one that does not.

    Growing the crew is the realistic case - a business with four times the work has
    more than six people. Holding it fixed answers a different question: how does the
    solver behave when it genuinely cannot serve everything?
    """
    results: list[LoadResult] = []
    for size in sizes:
        factor = max(1, round(size / 25))
        results.append(
            run_day(
                jobs=size,
                workers=6 * factor if scale_crew else 6,
                vans=4 * factor if scale_crew else 4,
            )
        )
    return results


def render(results: Sequence[LoadResult]) -> str:
    lines = [
        f"{'size':<34} {'solve':>8} {'served':>12} {'quote':>8} {'options':>8}",
        "-" * 78,
    ]
    for r in results:
        lines.append(
            f"{r.label:<34} {r.solve_seconds:>7.2f}s "
            f"{r.jobs_served:>4}/{r.jobs:<3} {r.served_fraction:>4.0%} "
            f"{r.quote_seconds:>7.2f}s {r.quote_options:>8}"
        )
        for note in r.notes:
            lines.append(f"{'':34} {note}")

    solves = [r.solve_seconds for r in results]
    quotes = [r.quote_seconds for r in results if r.quote_seconds]
    lines += ["", "-" * 78]
    if solves:
        lines.append(f"  slowest plan  {max(solves):.2f}s")
    if quotes:
        lines.append(
            f"  quote latency median {statistics.median(quotes):.2f}s, worst {max(quotes):.2f}s"
        )
        lines.append(
            "  a quote is given while a customer waits; past about three seconds the "
            "feature stops being worth having"
        )
    return "\n".join(lines)


def main() -> int:
    """Print each result as it lands.

    Buffering the whole sweep hides the one thing worth seeing: *which* configuration
    is slow. A run that produces nothing for ten minutes and then a table has told you
    less than one that stalls visibly on the row that stalled.
    """
    business = BusinessParams.load()
    print(f"LOAD  synthetic travel, {business.meta.timezone}")
    print("      budget 5s per solve; a solve that needs longer has already answered\n")

    header = f"{'size':<34} {'solve':>8} {'served':>12} {'quote':>8} {'status':>10}"

    def show(result: LoadResult) -> None:
        print(
            f"{result.label:<34} {result.solve_seconds:>7.2f}s "
            f"{result.jobs_served:>4}/{result.jobs:<3} {result.served_fraction:>4.0%} "
            f"{result.quote_seconds:>7.2f}s {result.status:>10}",
            flush=True,
        )
        for note in result.notes:
            print(f"{'':34} {note}", flush=True)

    print("Volume with a crew that grows to match it:")
    print(header)
    print("-" * 78)
    grown = []
    for size in (25, 50, 100):
        factor = max(1, round(size / 25))
        result = run_day(jobs=size, workers=6 * factor, vans=4 * factor)
        grown.append(result)
        show(result)

    print("\nVolume against today's roster, which cannot absorb it:")
    print(header)
    print("-" * 78)
    for size in (50, 100):
        show(run_day(jobs=size, workers=6, vans=4))

    print("\nA five-day horizon:")
    print(header)
    print("-" * 78)
    for size in (25, 60):
        show(run_horizon(jobs=size, workers=6, vans=4))

    quotes = [r.quote_seconds for r in grown if r.quote_seconds]
    if quotes:
        print("\n" + "-" * 78)
        print(f"  quote latency median {statistics.median(quotes):.2f}s, worst {max(quotes):.2f}s")
        print(
            "  a quote is given while a customer waits, so past about three seconds "
            "the feature stops being worth having"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
