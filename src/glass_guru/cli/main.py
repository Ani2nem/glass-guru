"""``glass-guru`` - inspection commands for the scheduling engine.

Exists so that looking at a plan does not require writing Python. Every command
prints to stdout and exits non-zero on a genuine problem, which makes them usable
both by hand and as snapshot tests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from glass_guru.cli import render
from glass_guru.config import BusinessParams
from glass_guru.domain.invariants import ValidationConfig, validate_plan
from glass_guru.domain.models import PlanVersion
from glass_guru.domain.state import WorldState, fold
from glass_guru.domain.travel import TravelOracle
from glass_guru.fixtures import scenarios
from glass_guru.fixtures.sample_business import WEEK_START, sample_world_at
from glass_guru.scheduler.costing import cost_plan
from glass_guru.scheduler.day_planner import DayPlanResult, SolveParams, plan_day
from glass_guru.scheduler.travel.base import OverrideAdjustedProvider
from glass_guru.scheduler.travel.synthetic import SyntheticTravelProvider


def _load_world(as_of: datetime | None = None) -> WorldState:
    """The sample business. Replaced by the Postgres event log in a later increment."""
    events, default_as_of = sample_world_at()
    return fold(events, as_of=as_of or default_as_of)


def _travel_for(world: WorldState, business: BusinessParams) -> TravelOracle:
    """Travel provider with the world's recorded traffic delays layered on top.

    Providers stay pure; a ``TrafficDelay`` event affects routing without any
    provider knowing the event log exists.
    """
    base = SyntheticTravelProvider.from_business(business)
    if not world.traffic_overrides:
        return base
    return OverrideAdjustedProvider(base, world.traffic_multiplier)


def _solve(
    world: WorldState,
    business: BusinessParams,
    on_date: date,
    *,
    allow_overtime: bool = True,
) -> tuple[DayPlanResult, PlanVersion, SolveParams]:
    tz = ZoneInfo(business.meta.timezone)
    travel = _travel_for(world, business)
    params = SolveParams.from_business(business, tz, allow_overtime=allow_overtime)
    result = plan_day(
        world=world,
        travel=travel,
        on_date=on_date,
        candidate_job_ids=[job.id for job in world.active_jobs()],
        params=params,
    )
    plan = PlanVersion(
        id=f"plan-{on_date.isoformat()}",
        created_at=world.as_of,
        horizon_start=on_date,
        horizon_end=on_date,
        routes=result.routes,
        unserved=result.unserved,
    )
    return result, plan, params


def cmd_solve(args: argparse.Namespace) -> int:
    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()
    on_date = args.date or WEEK_START

    result, plan, _ = _solve(world, business, on_date, allow_overtime=not args.no_overtime)
    travel = _travel_for(world, business)
    violations = validate_plan(
        plan, world, travel, ValidationConfig(business_tz=tz, allow_overtime=not args.no_overtime)
    )
    cost, route_costs = cost_plan(plan, world, business, tz, result.unserved)

    print(render.render_board(result, world, plan, business, tz, cost, route_costs, violations))
    # A plan that fails its own invariants is a failure, not a report.
    return 1 if violations else 0


def cmd_show(args: argparse.Namespace) -> int:
    business = BusinessParams.load(args.config)
    print(render.render_world(_load_world(), ZoneInfo(business.meta.timezone)))
    return 0


def cmd_params(args: argparse.Namespace) -> int:
    print(render.render_params(BusinessParams.load(args.config)))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Why is this job where it is - or why is it nowhere?"""
    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()
    on_date = args.date or WEEK_START

    job = world.jobs.get(args.job_id)
    if job is None:
        print(f"no such job: {args.job_id}", file=sys.stderr)
        return 2

    result, _, _ = _solve(world, business, on_date)
    print(f"{job.id}  {job.customer_name}  ({job.service_type.value})")
    print(render.RULE)
    print(f"  duration     {job.estimated_duration_min} min, crew of {job.crew_size}")
    print(f"  certs        {sorted(c.value for c in job.required_certifications) or 'none'}")
    print(f"  state        {job.commitment_state.value}   deferred {job.deferral_count}x")
    for window in job.windows:
        print(
            f"  window       {window.start.astimezone(tz):%a %d %b %H:%M}"
            f"-{window.end.astimezone(tz):%H:%M}  ({window.hardness.value})"
        )
    for material in job.materials:
        ready = material.available_from(job.requested_at.date())
        print(
            f"  material     {material.quantity}x {material.part_code}  "
            f"{'in stock' if material.in_stock else f'lead {material.lead_time_days}d'}"
            f"  ready {ready.isoformat()}"
        )
    print()

    for route in result.routes:
        for stop in route.stops:
            if stop.job_id != job.id:
                continue
            names = " + ".join(world.workers[w].name for w in route.worker_ids)
            print(
                f"  SCHEDULED    {on_date.isoformat()} on {route.crew_id} ({names}, {route.van_id})"
            )
            print(
                f"               {stop.arrival.astimezone(tz):%H:%M}"
                f"-{stop.departure.astimezone(tz):%H:%M}, "
                f"{stop.travel_minutes_from_prev} min drive to reach"
            )
            return 0

    for item in result.unserved:
        if item.job_id == job.id:
            print(f"  NOT SCHEDULED on {on_date.isoformat()}")
            print(f"               reason: {item.reason.value}")
            print(f"               {item.detail}")
            return 0

    print(f"  not a candidate for {on_date.isoformat()}")
    return 0


def _render_solve(
    world: WorldState,
    business: BusinessParams,
    on_date: date,
    *,
    allow_overtime: bool = True,
) -> tuple[str, int]:
    """Solve and render. Returns the board and an exit code (non-zero if infeasible)."""
    tz = ZoneInfo(business.meta.timezone)
    result, plan, _ = _solve(world, business, on_date, allow_overtime=allow_overtime)
    travel = _travel_for(world, business)
    violations = validate_plan(
        plan, world, travel, ValidationConfig(business_tz=tz, allow_overtime=allow_overtime)
    )
    cost, route_costs = cost_plan(plan, world, business, tz, result.unserved)
    board = render.render_board(result, world, plan, business, tz, cost, route_costs, violations)
    return board, (1 if violations else 0)


def cmd_scenario(args: argparse.Namespace) -> int:
    business = BusinessParams.load(args.config)
    if args.name in {"list", None}:
        for name, scenario in sorted(scenarios.SCENARIOS.items()):
            print(f"  {name:<22} {scenario.description}")
        return 0
    try:
        scenario = scenarios.get(args.name)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    world = scenario.world()
    print(f"SCENARIO  {scenario.name}")
    print(" ".join(scenario.description.split()))
    print()
    board, code = _render_solve(world, business, scenario.solve_date)
    print(board)
    return code


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glass-guru", description="Inspect the glass-guru scheduling engine."
    )
    parser.add_argument(
        "--config", default=None, help="path to business_params.yaml (default: repo config/)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="solve a day and render the crew board")
    solve.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD")
    solve.add_argument("--no-overtime", action="store_true", help="forbid overtime")
    solve.set_defaults(func=cmd_solve)

    show = sub.add_parser("show", help="show the world: workers, vans, jobs")
    show.set_defaults(func=cmd_show)

    params = sub.add_parser("params", help="business parameters and their provenance")
    params.set_defaults(func=cmd_params)

    scenario = sub.add_parser("scenario", help="run a named disruption scenario")
    scenario.add_argument("name", nargs="?", default="list", help="scenario name, or 'list'")
    scenario.set_defaults(func=cmd_scenario)

    explain = sub.add_parser("explain", help="why a job is scheduled where it is, or not at all")
    explain.add_argument("job_id")
    explain.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD")
    explain.set_defaults(func=cmd_explain)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
