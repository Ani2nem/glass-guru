"""Rendering plans as text a human can scan.

This is the piece that makes the system inspectable, and it exists early on purpose.
Of the bugs found while building the solver, two were caught by mypy and pytest and
three were caught by *reading route output* - including an objective that charged
travel labour per crew instead of per person, which every test happily passed.

So the board deliberately surfaces the things a validity checker cannot judge:
crew size against what each job needs, idle waiting, utilisation, and the solver's
estimate next to the plan's actual cost. Those columns are where "technically valid
but obviously wrong" becomes obvious.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo

from glass_guru.agents.a2a.types import Task
from glass_guru.config import BusinessParams, Provenance, calibration_banner
from glass_guru.domain.autonomy import AutonomyDecision, AutonomyPolicy, decide
from glass_guru.domain.diff import PlanDiff
from glass_guru.domain.enums import NOT_A_FAILURE
from glass_guru.domain.invariants import Violation
from glass_guru.domain.models import (
    CostBreakdown,
    CrewRoute,
    Job,
    PlanVersion,
    UnservedJob,
)
from glass_guru.domain.state import WorldState
from glass_guru.scheduler.booking import BookingOptions
from glass_guru.scheduler.costing import RouteCost
from glass_guru.scheduler.day_planner import DayPlanResult
from glass_guru.scheduler.horizon import HorizonResult
from glass_guru.scheduler.repair import RepairCandidate, RepairOptions

RULE = "-" * 78


def _hhmm(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def banner(params: BusinessParams) -> list[str]:
    warning = calibration_banner(params)
    return [] if warning is None else [f"!  {warning}", ""]


def _render_route(
    route: CrewRoute,
    world: WorldState,
    tz: tzinfo,
    rc: RouteCost | None,
    indent: str = "  ",
) -> list[str]:
    """One crew's day. The columns are chosen to expose what a validity checker
    cannot judge: crew size against need, waiting, and on-site share."""
    names = " + ".join(world.workers[w].name if w in world.workers else w for w in route.worker_ids)
    lines = [f"{indent}{route.crew_id}   {names}   [{route.van_id}]"]

    previous_departure: datetime | None = None
    for stop in route.stops:
        job = world.jobs.get(stop.job_id)
        if job is None:
            lines.append(f"{indent}    ?? unknown job {stop.job_id}")
            continue

        # Slack between jobs gets its own line. The crew leaves late rather than
        # idling on a doorstep, so the useful facts are how big the gap is and when
        # they actually pull away - not a "waiting" label that no longer describes
        # what the materializer does.
        if previous_departure is not None:
            free_at = previous_departure + timedelta(minutes=stop.travel_minutes_from_prev)
            gap = int((stop.arrival - free_at).total_seconds() // 60)
            if gap > 0:
                leaves = stop.arrival - timedelta(minutes=stop.travel_minutes_from_prev)
                lines.append(f"{indent}    {'':11}  ..{gap} min gap, leaves {_hhmm(leaves, tz)}")

        crew_note = f"needs {job.crew_size}" if job.crew_size > 1 else ""
        lines.append(
            f"{indent}    {_hhmm(stop.arrival, tz)}-{_hhmm(stop.departure, tz)}  "
            f"{job.customer_name:<26} {job.service_type.value:<30} "
            f"{stop.travel_minutes_from_prev:>3}min drive  {crew_note}"
        )
        previous_departure = stop.departure

    if rc is not None:
        lines.append(
            f"{indent}    {'':11}  travel {rc.travel_minutes}min / {rc.travel_miles:.1f}mi"
            f"  ({rc.person_travel_minutes} person-min)"
            f"   idle {rc.idle_minutes}min"
            f"   on-site {rc.utilization:.0%}"
            + (f"   OT {rc.overtime_minutes}min" if rc.overtime_minutes else "")
        )
    return lines


def _render_unserved(unserved: Sequence[UnservedJob], world: WorldState) -> list[str]:
    missed = [u for u in unserved if u.reason not in NOT_A_FAILURE]
    deferred = [u for u in unserved if u.reason in NOT_A_FAILURE]
    lines: list[str] = []

    def _list(title: str, items: Sequence[UnservedJob]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"  {title}")
        for item in items:
            job = world.jobs.get(item.job_id)
            label = job.customer_name if job else item.job_id
            lines.append(f"      {item.job_id}  {label:<26} {item.reason.value}")
            lines.append(f"      {'':11}  {item.detail}")

    # Split deliberately: "could not fit" is a problem to act on, "not in scope"
    # is routine. Showing them together buries the first in the second.
    _list("UNSERVED - could not fit", missed)
    _list("not in scope for this plan", deferred)
    return lines


def _render_cost(
    cost: CostBreakdown,
    objective: float,
    status: str,
    seconds: float,
) -> list[str]:
    drift = objective - cost.total
    return [
        "",
        RULE,
        f"  cost  travel {_money(cost.travel_labor)}"
        f"   vehicle {_money(cost.vehicle)}"
        f"   overtime {_money(cost.overtime)}"
        f"   late {_money(cost.lateness_penalty)}",
        f"        unserved {_money(cost.unserved_penalty)}   TOTAL {_money(cost.total)}",
        # Solver objective vs. materialized cost. Divergence means the pessimistic
        # matrix is drifting from what the routes actually do.
        f"  solver objective {_money(objective)}  "
        f"({'+' if drift >= 0 else ''}{_money(drift)} vs actual)   "
        f"status {status}   {seconds:.3f}s",
    ]


def _render_violations(violations: Sequence[Violation]) -> list[str]:
    if not violations:
        return ["", "  invariants: 0 violations"]
    out = ["", f"  INVARIANTS: {len(violations)} VIOLATION(S) - plan is not committable"]
    out += [f"      {v}" for v in violations]
    return out


def render_board(
    result: DayPlanResult,
    world: WorldState,
    plan: PlanVersion,
    business: BusinessParams,
    tz: tzinfo,
    cost: CostBreakdown,
    route_costs: Sequence[RouteCost],
    violations: Sequence[Violation] = (),
) -> str:
    """The crew-by-crew board for a single day."""
    lines: list[str] = []
    lines += banner(business)
    lines.append(f"PLAN {plan.horizon_start.isoformat()}  ({plan.content_hash})")
    lines.append(RULE)

    if not result.routes:
        lines.append("  no routes - nothing could be scheduled")

    for route, rc in zip(result.routes, route_costs, strict=False):
        lines.append("")
        lines += _render_route(route, world, tz, rc)

    lines += _render_unserved(result.unserved, world)
    lines += _render_cost(
        cost, result.objective_cost, result.status, result.metrics.get("solve_seconds", 0.0)
    )
    lines += _render_violations(violations)
    return "\n".join(lines)


def render_horizon(
    result: HorizonResult,
    world: WorldState,
    plan: PlanVersion,
    business: BusinessParams,
    tz: tzinfo,
    cost: CostBreakdown,
    route_costs: Sequence[RouteCost],
    violations: Sequence[Violation] = (),
) -> str:
    """The rolling multi-day board, grouped by day.

    Day totals are shown because the useful question across a horizon is usually
    "is Tuesday overloaded while Thursday sits empty", which per-crew lines alone
    do not answer.
    """
    lines: list[str] = []
    lines += banner(business)
    lines.append(
        f"HORIZON {plan.horizon_start.isoformat()}..{plan.horizon_end.isoformat()}"
        f"  ({plan.content_hash})"
    )
    lines.append(RULE)

    by_route = dict(zip([id(r) for r in result.routes], route_costs, strict=False))
    by_date: dict[date, list[CrewRoute]] = {}
    for route in result.routes:
        by_date.setdefault(route.date, []).append(route)

    for on_date in sorted(by_date):
        routes = by_date[on_date]
        jobs = sum(len(r.stops) for r in routes)
        costs = [by_route.get(id(r)) for r in routes]
        travel = sum(c.travel_minutes for c in costs if c)
        lines.append("")
        lines.append(
            f"  {on_date:%a %d %b}   {len(routes)} crew   {jobs} job(s)   {travel}min driving"
        )
        for route in routes:
            lines.append("")
            lines += _render_route(route, world, tz, by_route.get(id(route)), indent="    ")

    if not result.routes:
        lines.append("  no routes - nothing could be scheduled")

    lines += _render_unserved(result.unserved, world)
    objective = sum(r.objective_cost for r in result.day_results.values())
    status = (
        "OPTIMAL" if all(r.status == "OPTIMAL" for r in result.day_results.values()) else "MIXED"
    )
    lines += _render_cost(cost, objective, status, result.metrics.get("solve_seconds", 0.0))
    lines.append(
        f"  {int(result.metrics.get('scheduled', 0))}"
        f"/{int(result.metrics.get('candidates', 0))} jobs placed"
        f" across {int(result.metrics.get('days_used', 0))} day(s)"
        f"   {result.rounds} assignment round(s)"
    )
    lines += _render_violations(violations)
    return "\n".join(lines)


def render_world(world: WorldState, tz: tzinfo) -> str:
    lines = [f"WORLD as of {world.as_of.astimezone(tz).isoformat()}", RULE, "", "  WORKERS"]
    for worker in sorted(world.workers.values(), key=lambda w: w.id):
        certs = ", ".join(sorted(c.value for c in worker.certifications))
        hours = worker.hours_for(0)
        shift = f"{hours.start}-{hours.end}" if hours else "no Monday shift"
        ot = "" if worker.overtime_eligible else "  [no OT]"
        lines.append(f"      {worker.id:<12} {worker.name:<10} {shift:<14}{ot}")
        lines.append(f"      {'':12} {certs}")

    lines += ["", "  VANS"]
    for van in sorted(world.vans.values(), key=lambda v: v.id):
        stock = ", ".join(f"{k}x{v}" for k, v in sorted(van.stock.items()))
        lines.append(f"      {van.id:<12} {van.rack_slots:>2} slots   {stock}")

    lines += ["", "  JOBS"]
    for job in world.active_jobs():
        window = job.windows[0] if job.windows else None
        when = (
            f"{window.start.astimezone(tz):%a %H:%M}-{window.end.astimezone(tz):%H:%M}"
            f" {window.hardness.value}"
            if window
            else "any time"
        )
        lines.append(
            f"      {job.id:<8} {job.customer_name:<24} {job.service_type.value:<30}"
            f" {job.estimated_duration_min:>4}min  crew {job.crew_size}  {when}"
        )
        lines.append(
            f"      {'':8} {job.commitment_state.value:<10} "
            f"certs: {sorted(c.value for c in job.required_certifications) or 'none'}"
        )
    return "\n".join(lines)


def render_params(business: BusinessParams) -> str:
    """The provenance table. Its job is to make unvalidated numbers impossible to miss."""
    marker = {
        Provenance.ESTIMATED: "GUESS",
        Provenance.MEASURED: "meas.",
        Provenance.CONFIRMED: "conf.",
    }
    lines = [f"BUSINESS PARAMETERS  ({business.meta.name})", RULE, ""]
    for path, param in business.walk():
        lines.append(f"  {marker[param.source]:<6} {path:<44} {param.value:>10,.2f}")
        if param.note:
            wrapped = " ".join(param.note.split())
            lines.append(f"  {'':6} {'':44} {wrapped[:90]}")
    tally = business.counts()
    lines += [
        "",
        RULE,
        f"  {tally[Provenance.ESTIMATED]} estimated   "
        f"{tally[Provenance.MEASURED]} measured   "
        f"{tally[Provenance.CONFIRMED]} confirmed",
    ]
    warning = calibration_banner(business)
    if warning:
        lines.append(f"  !  {warning}")
    return "\n".join(lines)


def render_slots(
    options: BookingOptions,
    draft: Job,
    business: BusinessParams,
    tz: tzinfo,
) -> str:
    """Bookable slots, cheapest first.

    Ordered by what serving the job actually costs, so "Tuesday afternoon" is a
    priced recommendation rather than a preference. The spread at the bottom is the
    number that makes the feature worth having: it is what choosing well is worth on
    this one call.
    """
    lines: list[str] = []
    lines += banner(business)
    lines.append(
        f"SLOTS for {draft.customer_name} - {draft.service_type.value}, "
        f"{draft.estimated_duration_min}min, crew of {draft.crew_size}"
    )
    if draft.location.address:
        lines.append(f"  at {draft.location.address}")
    lines.append(RULE)

    if not options.slots:
        lines.append("")
        lines.append("  no bookable slot in the horizon")
    for index, slot in enumerate(options.slots, start=1):
        marker = "*" if index == 1 else " "
        start = slot.quoted_window.start.astimezone(tz)
        end = slot.quoted_window.end.astimezone(tz)
        crew = " + ".join(slot.worker_names) or slot.crew_id
        lines.append("")
        lines.append(
            f" {marker} {start:%a %d %b}  {start:%H:%M}-{end:%H:%M}   "
            f"{_money(slot.marginal_cost):>9}   {crew}"
        )
        lines.append(f"      {slot.reason}")

    if options.unavailable:
        lines.append("")
        lines.append("  not bookable")
        for item in options.unavailable:
            lines.append(f"      {item.on_date:%a %d %b}  {item.reason.value}: {item.detail}")

    lines.append("")
    lines.append(RULE)
    if len(options.slots) > 1:
        lines.append(
            f"  {len(options.slots)} option(s) across {options.evaluated_days} days."
            f"  Booking the cheapest rather than the dearest saves "
            f"{_money(options.savings_vs_worst)}."
        )
    return "\n".join(lines)


def render_repair(
    options: RepairOptions,
    recommended: RepairCandidate | None,
    world: WorldState,
    business: BusinessParams,
    tz: tzinfo,
    policy: AutonomyPolicy,
) -> str:
    """Repair candidates, side by side, with what each would cost in phone calls.

    Deliberately not a single answer. Choosing between "keep every promise and serve
    less" and "serve more and make two calls" is a judgement about this business on
    this day; the engine's job is to price the options honestly.
    """
    lines: list[str] = []
    lines += banner(business)
    lines.append(f"REPAIR of {options.baseline.id} ({options.baseline.content_hash})")
    lines.append(RULE)

    for candidate in options.candidates:
        decision = decide(candidate.diff, policy)
        marker = "*" if candidate is recommended else " "
        lines.append("")
        lines.append(
            f" {marker} {candidate.strategy.name:<18} "
            f"{candidate.jobs_served:>2} served   "
            f"{candidate.changes:>2} change(s)   "
            f"{candidate.customer_calls} call(s)   "
            f"[{candidate.diff.blast_radius.value}]"
        )
        lines.append(f"      {candidate.strategy.description}")
        for change in candidate.diff.changes[:6]:
            flag = "CALL" if change.customer_visible else "    "
            lines.append(f"      {flag} {change.describe()}")
        if len(candidate.diff.changes) > 6:
            lines.append(f"      ... {len(candidate.diff.changes) - 6} more")
        lines.append(f"      -> {decision.explain()}")

    lines.append("")
    lines.append(RULE)
    if recommended is None:
        lines.append("  no repair candidate available")
    else:
        lines.append(
            f"  recommended: {recommended.strategy.name}"
            f"  ({recommended.jobs_served} served, {recommended.customer_calls} call(s))"
        )
    return "\n".join(lines)


def render_diff(
    diff: PlanDiff,
    before: PlanVersion,
    after: PlanVersion,
    decision: AutonomyDecision,
) -> str:
    lines = [
        f"DIFF {before.id} ({before.content_hash}) -> {after.id} ({after.content_hash})",
        RULE,
        "",
        f"  {diff.summary()}",
        f"  blast radius: {diff.blast_radius.value}",
        "",
    ]
    for change in diff.changes:
        flag = "CALL" if change.customer_visible else "    "
        lines.append(f"  {flag} {change.describe()}")
    if not diff.changes:
        lines.append("  (identical)")
    lines += ["", RULE, f"  {decision.explain()}"]
    return "\n".join(lines)


def render_triage(task: Task, payload: dict[str, object]) -> str:
    """What the agent made of a note, and what still needs a person.

    Repairs are shown because they are the health signal worth watching with a small
    model: a rising repair rate means it is drifting from what the prompt and schema
    expect, long before anyone notices a bad schedule.
    """
    lines = [RULE, f"  task {task.id}   state: {task.state.value}"]
    if summary := payload.get("summary"):
        lines.append(f"  {summary}")

    events = payload.get("events") or []
    if isinstance(events, list) and events:
        lines.append("")
        lines.append("  EVENTS")
        for event in events:
            if not isinstance(event, dict):
                continue
            target = event.get("van_id") or event.get("worker_id") or event.get("job_id") or ""
            lines.append(f"      {event.get('type', '?'):<22} {target}")
            if reason := event.get("reason"):
                lines.append(f"      {'':22} {reason}")

    for label, key in (("unrecognised ids", "unknown_targets"), ("could not record", "rejected")):
        values = payload.get(key) or []
        if isinstance(values, list) and values:
            lines.append("")
            lines.append(f"  {label}: {', '.join(str(v) for v in values)}")

    if task.needs_input and task.status.message:
        lines += ["", f"  NEEDS A DISPATCHER: {task.status.message.text_content}"]

    repairs = payload.get("repairs")
    if isinstance(repairs, int) and repairs:
        lines += ["", f"  note: the model needed {repairs} repair attempt(s)"]
    lines.append(RULE)
    return "\n".join(lines)
