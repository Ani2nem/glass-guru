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
from datetime import datetime, tzinfo

from glass_guru.config import BusinessParams, Provenance, calibration_banner
from glass_guru.domain.enums import NOT_A_FAILURE
from glass_guru.domain.invariants import Violation
from glass_guru.domain.models import CostBreakdown, PlanVersion, UnservedJob
from glass_guru.domain.state import WorldState
from glass_guru.scheduler.costing import RouteCost
from glass_guru.scheduler.day_planner import DayPlanResult

RULE = "-" * 78


def _hhmm(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def banner(params: BusinessParams) -> list[str]:
    warning = calibration_banner(params)
    return [] if warning is None else [f"!  {warning}", ""]


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
    """The crew-by-crew day board."""
    lines: list[str] = []
    lines += banner(business)
    lines.append(f"PLAN {plan.horizon_start.isoformat()}  ({plan.content_hash})")
    lines.append(RULE)

    if not result.routes:
        lines.append("  no routes - nothing could be scheduled")

    by_crew = {rc.crew_id: rc for rc in route_costs}
    for route in result.routes:
        names = " + ".join(
            world.workers[w].name if w in world.workers else w for w in route.worker_ids
        )
        rc = by_crew.get(route.crew_id)
        lines.append("")
        lines.append(f"  {route.crew_id}   {names}   [{route.van_id}]")

        previous_departure: datetime | None = None
        for stop in route.stops:
            job = world.jobs.get(stop.job_id)
            if job is None:
                lines.append(f"      ?? unknown job {stop.job_id}")
                continue

            # Waiting shows up as its own line: arriving early and sitting outside a
            # closed door is a real cost the timestamps alone would hide.
            if previous_departure is not None:
                ready = previous_departure.timestamp() + stop.travel_minutes_from_prev * 60
                waited = int((stop.arrival.timestamp() - ready) // 60)
                if waited > 0:
                    lines.append(f"      {'':11}  ..waiting {waited} min")

            crew_note = f"needs {job.crew_size}" if job.crew_size > 1 else ""
            lines.append(
                f"      {_hhmm(stop.arrival, tz)}-{_hhmm(stop.departure, tz)}  "
                f"{job.customer_name:<26} {job.service_type.value:<30} "
                f"{stop.travel_minutes_from_prev:>3}min drive  {crew_note}"
            )
            previous_departure = stop.departure

        if rc is not None:
            lines.append(
                f"      {'':11}  travel {rc.travel_minutes}min / {rc.travel_miles:.1f}mi"
                f"  ({rc.person_travel_minutes} person-min)"
                f"   idle {rc.idle_minutes}min"
                f"   on-site {rc.utilization:.0%}"
                + (f"   OT {rc.overtime_minutes}min" if rc.overtime_minutes else "")
            )

    missed = [u for u in result.unserved if u.reason not in NOT_A_FAILURE]
    deferred = [u for u in result.unserved if u.reason in NOT_A_FAILURE]

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

    # Split deliberately: "could not fit" is a problem to act on, "not today's work"
    # is routine. Showing them together buries the first in the second.
    _list("UNSERVED - could not fit", missed)
    _list("not this day's work", deferred)

    lines.append("")
    lines.append(RULE)
    lines.append(
        f"  cost  travel {_money(cost.travel_labor)}"
        f"   vehicle {_money(cost.vehicle)}"
        f"   overtime {_money(cost.overtime)}"
        f"   late {_money(cost.lateness_penalty)}"
    )
    lines.append(f"        unserved {_money(cost.unserved_penalty)}   TOTAL {_money(cost.total)}")
    # Solver objective vs. materialized cost. Divergence means the pessimistic matrix
    # is drifting from what the routes actually do.
    drift = result.objective_cost - cost.total
    lines.append(
        f"  solver objective {_money(result.objective_cost)}  "
        f"({'+' if drift >= 0 else ''}{_money(drift)} vs actual)   "
        f"status {result.status}   {result.metrics.get('solve_seconds', 0):.3f}s"
    )

    lines.append("")
    if violations:
        lines.append(f"  INVARIANTS: {len(violations)} VIOLATION(S) - plan is not committable")
        for violation in violations:
            lines.append(f"      {violation}")
    else:
        lines.append("  invariants: 0 violations")
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
