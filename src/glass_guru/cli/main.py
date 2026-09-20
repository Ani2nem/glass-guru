"""``glass-guru`` - inspection commands for the scheduling engine.

Exists so that looking at a plan does not require writing Python. Every command
prints to stdout and exits non-zero on a genuine problem, which makes them usable
both by hand and as snapshot tests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from glass_guru.cli import render
from glass_guru.cli.events import KINDS, EventArgumentError, build_event
from glass_guru.config import BusinessParams
from glass_guru.domain.autonomy import AutonomyPolicy, decide
from glass_guru.domain.diff import diff_plans
from glass_guru.domain.enums import Certification, ServiceType
from glass_guru.domain.invariants import ValidationConfig, summarize, validate_plan
from glass_guru.domain.models import Job, Location, PlanVersion, TimeWindow
from glass_guru.domain.state import WorldState, fold
from glass_guru.domain.travel import TravelOracle
from glass_guru.fixtures import scenarios
from glass_guru.fixtures.sample_business import WEEK_START, sample_world_at, seed_events
from glass_guru.geocoding import GeocodeError, Geocoder
from glass_guru.obs.correlation import dispatch, require_dispatch_id
from glass_guru.obs.tracing import configure, span
from glass_guru.persistence.log import PlanConflict, Workspace
from glass_guru.scheduler.booking import suggest_booking_slots
from glass_guru.scheduler.costing import cost_plan
from glass_guru.scheduler.day_planner import DayPlanResult, SolveParams, plan_day
from glass_guru.scheduler.horizon import HorizonParams, HorizonResult, plan_horizon
from glass_guru.scheduler.repair import repair_plan
from glass_guru.scheduler.travel.base import OverrideAdjustedProvider, TravelProvider
from glass_guru.scheduler.travel.factory import TravelMode, build_travel, describe

#: Workspace directory for this process, set once from --workspace.
_WORKSPACE = Path(".glass-guru")


def _workspace() -> Workspace:
    return Workspace(_WORKSPACE)


def _load_world(as_of: datetime | None = None) -> WorldState:
    """World state from the workspace log if one exists, otherwise the sample fixture.

    Falling back keeps read-only commands working in a fresh clone, while anything
    that mutates state requires an initialised workspace - state that vanishes when
    the process exits is worse than no state at all.
    """
    workspace = _workspace()
    if workspace.exists:
        # Fold the whole log: the latest recorded event is "now" for a replayed world.
        return fold(workspace.events.read(), as_of=as_of)
    events, default_as_of = sample_world_at()
    return fold(events, as_of=as_of or default_as_of)


#: Travel source for this process, set once from --travel.
_TRAVEL_MODE: TravelMode = TravelMode.AUTO


def _travel_for(world: WorldState, business: BusinessParams) -> TravelOracle:
    """Travel provider with the world's recorded traffic delays layered on top.

    Providers stay pure; a ``TrafficDelay`` event affects routing without any
    provider knowing the event log exists.
    """
    base = build_travel(business, _TRAVEL_MODE)
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


def _render_horizon(
    world: WorldState,
    business: BusinessParams,
    start: date,
    *,
    days: int | None = None,
    allow_overtime: bool = True,
) -> tuple[str, int]:
    """Plan a rolling horizon and render it. Non-zero exit means infeasible."""
    tz = ZoneInfo(business.meta.timezone)
    travel = _travel_for(world, business)
    params = SolveParams.from_business(business, tz, allow_overtime=allow_overtime)
    horizon_params = HorizonParams.from_business(business)
    if days is not None:
        horizon_params = replace(horizon_params, days=days)

    result: HorizonResult = plan_horizon(
        world=world, travel=travel, start=start, params=params, horizon_params=horizon_params
    )
    end = date.fromordinal(start.toordinal() + horizon_params.days - 1)
    plan = PlanVersion(
        id=f"horizon-{start.isoformat()}",
        created_at=world.as_of,
        horizon_start=start,
        horizon_end=end,
        routes=result.routes,
        unserved=result.unserved,
    )
    violations = validate_plan(
        plan, world, travel, ValidationConfig(business_tz=tz, allow_overtime=allow_overtime)
    )
    cost, route_costs = cost_plan(plan, world, business, tz, result.unserved)
    board = render.render_horizon(result, world, plan, business, tz, cost, route_costs, violations)
    return board, (1 if violations else 0)


def cmd_solve(args: argparse.Namespace) -> int:
    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()
    on_date = args.date or WEEK_START

    if args.horizon:
        days = None if args.horizon is True else int(args.horizon)
        board, code = _render_horizon(
            world, business, on_date, days=days, allow_overtime=not args.no_overtime
        )
        print(board)
        return code

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


def cmd_slots(args: argparse.Namespace) -> int:
    """Price a prospective job into every day of the horizon.

    This is the question a dispatcher actually has on a call: not "what is the
    optimal schedule" but "when can I offer, and what does each option cost us?"
    """
    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()

    geocoder = Geocoder()
    if args.lat is not None and args.lon is not None:
        location = Location(lat=args.lat, lon=args.lon, address=args.address or "")
    elif args.address:
        try:
            location = geocoder.geocode(args.address)
        except GeocodeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        print("give --address or both --lat and --lon", file=sys.stderr)
        return 2

    start = args.date or WEEK_START
    horizon_params = HorizonParams.from_business(business)
    horizon = [
        date.fromordinal(start.toordinal() + offset) for offset in range(horizon_params.days)
    ]

    draft = Job(
        id="draft",
        customer_id="draft",
        customer_name=args.customer,
        location=location,
        service_type=ServiceType(args.service),
        required_certifications=frozenset(Certification(c) for c in args.certs),
        crew_size=args.crew,
        estimated_duration_min=args.duration,
        revenue=args.revenue,
        windows=tuple(
            TimeWindow(
                start=datetime.combine(day, time(8, 0), tzinfo=tz),
                end=datetime.combine(day, time(17, 0), tzinfo=tz),
            )
            for day in horizon
        ),
        requested_at=world.as_of,
    )

    travel = _travel_for(world, business)
    params = SolveParams.from_business(business, tz)
    options = suggest_booking_slots(
        world=world,
        travel=travel,
        draft=draft,
        horizon=horizon,
        params=params,
        business=business,
    )
    print(render.render_slots(options, draft, business, tz))
    return 0 if options.slots else 1


def _require_workspace() -> Workspace | None:
    workspace = _workspace()
    if not workspace.exists:
        print(
            f"no workspace at {workspace.root}. Create one with:  glass-guru init",
            file=sys.stderr,
        )
        return None
    return workspace


def cmd_init(args: argparse.Namespace) -> int:
    """Seed a workspace from the sample business."""
    workspace = _workspace()
    try:
        count = workspace.seed(seed_events())
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"seeded {workspace.root} with {count} events")
    print(workspace.describe())
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    """Append one typed event to the log."""
    workspace = _require_workspace()
    if workspace is None:
        return 2

    on_date = args.on or WEEK_START
    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)

    def moment(clock: str | None, fallback: time) -> datetime:
        parsed = datetime.strptime(clock, "%H:%M").time() if clock else fallback
        return datetime.combine(on_date, parsed, tzinfo=tz)

    try:
        event = build_event(
            args.kind,
            args.target,
            at=moment(args.at, time(8, 0)),
            dispatch_id=require_dispatch_id(),
            until=moment(args.until, time(17, 0)) if args.until else None,
            window_start=moment(args.window_start, time(8, 0)) if args.window_start else None,
            window_end=moment(args.window_end, time(17, 0)) if args.window_end else None,
            minutes=args.minutes,
            multiplier=args.multiplier,
            commitment_cost=args.commitment_cost,
            reason=args.reason,
        )
    except (EventArgumentError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    workspace.events.append([event])
    print(f"recorded {event.type}  {event.event_id}  dispatch={event.dispatch_id}")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """Plan the horizon and commit it as the new head."""
    workspace = _require_workspace()
    if workspace is None:
        return 2

    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()
    start = args.date or WEEK_START

    head = workspace.plans.head()
    travel = _travel_for(world, business)
    params = SolveParams.from_business(business, tz)
    horizon_params = HorizonParams.from_business(business)
    result = plan_horizon(
        world=world, travel=travel, start=start, params=params, horizon_params=horizon_params
    )
    plan = PlanVersion(
        id=f"v{len(workspace.plans.history()) + 1:03d}",
        parent_id=head.id if head else None,
        created_at=world.as_of,
        horizon_start=start,
        horizon_end=date.fromordinal(start.toordinal() + horizon_params.days - 1),
        routes=result.routes,
        unserved=result.unserved,
        label="committed",
    )

    violations = validate_plan(plan, world, travel, ValidationConfig(business_tz=tz))
    if violations:
        # A plan that fails its own invariants never reaches storage. This is the
        # runtime guard, not a test.
        print(summarize(violations), file=sys.stderr)
        return 1

    try:
        workspace.plans.commit(plan, expected_parent=head.id if head else None)
    except PlanConflict as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if head is not None:
        print(diff_plans(head, plan, world).summary())
    print(f"committed {plan.id} ({plan.content_hash}), {len(result.scheduled_job_ids)} jobs")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Repair the committed plan after a disruption, and say whether a human is needed."""
    workspace = _require_workspace()
    if workspace is None:
        return 2
    head = workspace.plans.head()
    if head is None:
        print("nothing committed yet; run:  glass-guru commit", file=sys.stderr)
        return 2

    business = BusinessParams.load(args.config)
    tz = ZoneInfo(business.meta.timezone)
    world = _load_world()
    travel = _travel_for(world, business)
    params = SolveParams.from_business(business, tz)
    horizon_params = HorizonParams.from_business(business)
    policy = AutonomyPolicy.from_business(business)

    options = repair_plan(
        world=world,
        travel=travel,
        baseline=head,
        start=head.horizon_start,
        params=params,
        horizon_params=horizon_params,
    )
    recommended = options.best_by_fewest_calls
    print(render.render_repair(options, recommended, world, business, tz, policy))

    if not args.apply or recommended is None:
        return 0

    decision = decide(recommended.diff, policy)
    if not decision.auto and not args.force:
        print(f"\nnot applied - {decision.explain()}")
        print("re-run with --force to apply anyway, once a dispatcher has agreed")
        return 1

    violations = validate_plan(recommended.plan, world, travel, ValidationConfig(business_tz=tz))
    if violations:
        print(summarize(violations), file=sys.stderr)
        return 1
    try:
        workspace.plans.commit(recommended.plan, expected_parent=head.id)
    except PlanConflict as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(f"\napplied {recommended.plan.id} - {decision.explain()}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    workspace = _require_workspace()
    if workspace is None:
        return 2
    print(workspace.describe())
    for plan in workspace.plans.ancestry():
        jobs = sum(len(r.stops) for r in plan.routes)
        print(
            f"  {plan.id:<8} {plan.content_hash}  {plan.created_at:%Y-%m-%d %H:%M}  "
            f"{jobs:>3} job(s)  {plan.label or ''}"
        )
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    workspace = _require_workspace()
    if workspace is None:
        return 2
    chain = workspace.plans.ancestry()
    if len(chain) < 2 and not (args.before and args.after):
        print("need at least two plan versions to diff", file=sys.stderr)
        return 2

    before = workspace.plans.get(args.before) if args.before else chain[1]
    after = workspace.plans.get(args.after) if args.after else chain[0]
    if before is None or after is None:
        print("no such plan version", file=sys.stderr)
        return 2

    business = BusinessParams.load(args.config)
    world = _load_world()
    policy = AutonomyPolicy.from_business(business)
    diff = diff_plans(before, after, world)
    print(render.render_diff(diff, before, after, decide(diff, policy)))
    return 0


def cmd_travel(args: argparse.Namespace) -> int:
    """Report the travel source, and optionally compare the three against each other."""
    business = BusinessParams.load(args.config)
    world = _load_world()
    print(f"travel source: {describe(_TRAVEL_MODE)}")

    if not args.compare:
        oracle = build_travel(business, _TRAVEL_MODE)
        depot = world.vans["van-1"].home_depot
        probe = next(iter(world.active_jobs()))
        leg = oracle.leg(depot, probe.location, world.as_of)
        print(f"  sample leg depot -> {probe.customer_name}: {leg.minutes}min / {leg.miles}mi")
        return 0

    from glass_guru.scheduler.travel.cache import CacheMiss
    from glass_guru.scheduler.travel.osrm import OsrmUnavailable

    modes: list[tuple[str, TravelProvider]] = []
    for mode in (TravelMode.SYNTHETIC, TravelMode.FROZEN, TravelMode.OSRM):
        try:
            modes.append((mode.value, build_travel(business, mode)))
        except FileNotFoundError as exc:
            print(f"  {mode.value}: unavailable ({exc.args[0].splitlines()[0]})")

    depot = world.vans["van-1"].home_depot
    at = world.as_of
    header = "  ".join(f"{name:>18}" for name, _ in modes)
    print()
    print(f"{'job':<8} {'customer':<22} {header}")
    for job in world.active_jobs():
        cells = []
        for _, oracle in modes:
            try:
                leg = oracle.leg(depot, job.location, at)
                cells.append(f"{leg.minutes:>7}min {leg.miles:>6.1f}mi")
            except (CacheMiss, OsrmUnavailable):
                cells.append(f"{'unavailable':>18}")
        print(f"{job.id:<8} {job.customer_name:<22} {'  '.join(cells)}")
    return 0


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glass-guru", description="Inspect the glass-guru scheduling engine."
    )
    parser.add_argument(
        "--config", default=None, help="path to business_params.yaml (default: repo config/)"
    )
    parser.add_argument(
        "--dispatch-id",
        default=None,
        help="correlation id to trace under; minted per invocation when omitted",
    )
    parser.add_argument(
        "--workspace",
        default=".glass-guru",
        help="directory holding the event log and plan history",
    )
    parser.add_argument(
        "--travel",
        choices=[m.value for m in TravelMode],
        default=TravelMode.AUTO.value,
        help="travel-time source (default: auto - frozen snapshot when present)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="solve a day and render the crew board")
    solve.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD")
    solve.add_argument("--no-overtime", action="store_true", help="forbid overtime")
    solve.add_argument(
        "--horizon",
        nargs="?",
        const=True,
        default=None,
        metavar="DAYS",
        help="plan a rolling horizon instead of one day (default: config value)",
    )
    solve.set_defaults(func=cmd_solve)

    show = sub.add_parser("show", help="show the world: workers, vans, jobs")
    show.set_defaults(func=cmd_show)

    params = sub.add_parser("params", help="business parameters and their provenance")
    params.set_defaults(func=cmd_params)

    init = sub.add_parser("init", help="create a workspace seeded with the sample business")
    init.set_defaults(func=cmd_init)

    event = sub.add_parser("event", help="record a disruption or status change")
    event.add_argument("kind", choices=KINDS)
    event.add_argument("target", nargs="?", default=None, help="van, worker or job id")
    event.add_argument("--at", default=None, metavar="HH:MM", help="when it happened")
    event.add_argument("--until", default=None, metavar="HH:MM")
    event.add_argument("--on", type=_parse_date, default=None, help="date, default Monday")
    event.add_argument("--window-start", default=None, metavar="HH:MM")
    event.add_argument("--window-end", default=None, metavar="HH:MM")
    event.add_argument("--minutes", type=int, default=None)
    event.add_argument("--multiplier", type=float, default=None)
    event.add_argument("--commitment-cost", type=float, default=0.0)
    event.add_argument("--reason", default="")
    event.set_defaults(func=cmd_event)

    commit = sub.add_parser("commit", help="plan the horizon and commit it as the head")
    commit.add_argument("--date", type=_parse_date, default=None)
    commit.set_defaults(func=cmd_commit)

    repair = sub.add_parser("repair", help="repair the committed plan after a disruption")
    repair.add_argument("--apply", action="store_true", help="commit the recommendation")
    repair.add_argument("--force", action="store_true", help="apply even if it needs review")
    repair.set_defaults(func=cmd_repair)

    history = sub.add_parser("history", help="plan versions, newest first")
    history.set_defaults(func=cmd_history)

    diff = sub.add_parser("diff", help="compare two plan versions")
    diff.add_argument("before", nargs="?", default=None)
    diff.add_argument("after", nargs="?", default=None)
    diff.set_defaults(func=cmd_diff)

    slots = sub.add_parser("slots", help="price a prospective job into the horizon")
    slots.add_argument("--address", default=None, help="street address to geocode")
    slots.add_argument("--lat", type=float, default=None)
    slots.add_argument("--lon", type=float, default=None)
    slots.add_argument(
        "--service",
        default=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT.value,
        choices=[s.value for s in ServiceType],
    )
    slots.add_argument("--certs", nargs="*", default=[], choices=[c.value for c in Certification])
    slots.add_argument("--duration", type=int, default=90, help="minutes on site")
    slots.add_argument("--crew", type=int, default=1, choices=[1, 2])
    slots.add_argument("--revenue", type=float, default=700.0)
    slots.add_argument("--customer", default="New caller")
    slots.add_argument("--date", type=_parse_date, default=None, help="horizon start")
    slots.set_defaults(func=cmd_slots)

    travel = sub.add_parser("travel", help="show or compare the travel-time source")
    travel.add_argument(
        "--compare", action="store_true", help="compare synthetic, frozen and live OSRM"
    )
    travel.set_defaults(func=cmd_travel)

    scenario = sub.add_parser("scenario", help="run a named disruption scenario")
    scenario.add_argument("name", nargs="?", default="list", help="scenario name, or 'list'")
    scenario.set_defaults(func=cmd_scenario)

    explain = sub.add_parser("explain", help="why a job is scheduled where it is, or not at all")
    explain.add_argument("job_id")
    explain.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD")
    explain.set_defaults(func=cmd_explain)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global _TRAVEL_MODE, _WORKSPACE
    args = build_parser().parse_args(argv)
    _TRAVEL_MODE = TravelMode(args.travel)
    _WORKSPACE = Path(args.workspace)

    configure(service="glass-guru-cli")
    # One id per invocation, so a command's solves, commits and writes are one trace -
    # the same shape an agent's episode will have.
    with (
        dispatch(args.dispatch_id) as dispatch_id,
        span(f"cli.{args.command}", dispatch_id=dispatch_id),
    ):
        result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
