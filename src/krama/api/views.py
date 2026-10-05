"""Turning domain objects into what the board draws.

Kept apart from the endpoints so the mapping is testable without HTTP, and apart from
the domain so no view concern - minutes from midnight, hex colours, display names -
leaks into the model the solver reasons about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from krama.api.models import (
    CandidateView,
    ChangeView,
    CostView,
    CrewAskView,
    JobView,
    OvertimeOfferView,
    PlanView,
    RouteView,
    StopView,
    UnservedView,
    VanView,
    WorkerDayView,
    WorkerView,
    WorldView,
)
from krama.config import BusinessParams, calibration_banner
from krama.domain.autonomy import AutonomyDecision
from krama.domain.diff import PlanDiff
from krama.domain.enums import NOT_A_FAILURE
from krama.domain.invariants import Violation
from krama.domain.models import CostBreakdown, CrewRoute, Job, PlanVersion, Worker
from krama.domain.state import WorldState
from krama.formatting import clock, clock_range
from krama.scheduler.costing import RouteCost
from krama.scheduler.repair import RepairCandidate, RepairOptions


def _minute_of_day(moment: datetime, tz: tzinfo) -> int:
    local = moment.astimezone(tz)
    return local.hour * 60 + local.minute


def route_view(
    route: CrewRoute, world: WorldState, cost: RouteCost | None, tz: tzinfo
) -> RouteView:
    stops: list[StopView] = []
    previous_departure: datetime | None = None

    # When any member of the crew runs past their shift, the time is overtime. The
    # earliest close among the crew is the moment that starts.
    closes: list[int] = []
    for worker_id in route.worker_ids:
        worker = world.workers.get(worker_id)
        hours = worker.hours_for(route.date.weekday()) if worker else None
        if hours is not None:
            closes.append(hours.end.hour * 60 + hours.end.minute)
    shift_close = min(closes) if closes else 24 * 60

    previous_name = "the shop"
    for stop in route.stops:
        job = world.jobs.get(stop.job_id)
        gap = 0
        if previous_departure is not None:
            free_at = previous_departure + timedelta(minutes=stop.travel_minutes_from_prev)
            gap = max(0, int((stop.arrival - free_at).total_seconds() // 60))
        previous_departure = stop.departure

        stops.append(
            StopView(
                job_id=stop.job_id,
                customer_name=job.customer_name if job else stop.job_id,
                service_type=job.service_type.value if job else "",
                arrival=stop.arrival.astimezone(tz).isoformat(),
                departure=stop.departure.astimezone(tz).isoformat(),
                start_minute=_minute_of_day(stop.arrival, tz),
                end_minute=_minute_of_day(stop.departure, tz),
                travel_minutes=stop.travel_minutes_from_prev,
                travel_miles=round(stop.travel_miles_from_prev, 2),
                crew_size=job.crew_size if job else 1,
                commitment_state=job.commitment_state.value if job else "",
                gap_minutes=gap,
                lat=job.location.lat if job else 0.0,
                lon=job.location.lon if job else 0.0,
                past_shift=_minute_of_day(stop.departure, tz) > shift_close,
                from_label=previous_name,
            )
        )
        previous_name = f"{job.customer_name}'s" if job else previous_name

    return RouteView(
        crew_id=route.crew_id,
        date=route.date.isoformat(),
        worker_names=[world.workers[w].name if w in world.workers else w for w in route.worker_ids],
        van_id=route.van_id,
        stops=stops,
        travel_minutes=cost.travel_minutes if cost else route.total_travel_minutes,
        travel_miles=round(cost.travel_miles if cost else route.total_travel_miles, 1),
        idle_minutes=cost.idle_minutes if cost else 0,
        utilization=round(cost.utilization, 3) if cost else 0.0,
        overtime_minutes=cost.overtime_minutes if cost else 0,
    )


def plan_view(
    plan: PlanVersion,
    world: WorldState,
    cost: CostBreakdown,
    route_costs: list[RouteCost],
    violations: tuple[Violation, ...],
    tz: tzinfo,
) -> PlanView:
    by_route = dict(zip([id(r) for r in plan.routes], route_costs, strict=False))
    depot = next(iter(sorted(world.vans.values(), key=lambda v: v.id)), None)
    return PlanView(
        plan_id=plan.id,
        content_hash=plan.content_hash,
        horizon_start=plan.horizon_start.isoformat(),
        horizon_end=plan.horizon_end.isoformat(),
        routes=[route_view(r, world, by_route.get(id(r)), tz) for r in plan.routes],
        unserved=[
            UnservedView(
                job_id=u.job_id,
                customer_name=(j.customer_name if (j := world.jobs.get(u.job_id)) else ""),
                reason=u.reason.value,
                detail=u.detail,
                # The board separates "we could not fit this" from "not this plan's
                # work". Showing them together buries the first in the second.
                is_failure=u.reason not in NOT_A_FAILURE,
            )
            for u in plan.unserved
        ],
        cost=CostView(
            vehicle=round(cost.vehicle, 2),
            overtime=round(cost.overtime, 2),
            lateness=round(cost.lateness_penalty, 2),
            unserved=round(cost.unserved_penalty, 2),
            total=round(cost.total, 2),
        ),
        feasible=not violations,
        violations=[str(v) for v in violations],
        depot=[depot.home_depot.lat, depot.home_depot.lon] if depot else [],
    )


def _window_text(job: Job, tz: tzinfo) -> str:
    """The promised window as a person reads it: `Mon 9:00 - 11:30 AM hard`."""
    window = job.windows[0]
    start, end = window.start.astimezone(tz), window.end.astimezone(tz)
    return f"{start:%a} {clock_range(start, end)} {window.hardness.value}"


def _worker_week(
    worker: Worker, world: WorldState, business: BusinessParams, tz: tzinfo
) -> list[WorkerDayView]:
    """The week ahead for one fitter, as a dispatcher reads it.

    This is the panel that answers "why does the machine keep choosing Dan": the shift
    says when they work, the reach says how late overtime may keep them, and together
    with the certification chips the after-four decision stops being a mystery and
    becomes a rota anyone can read.
    """
    overtime = int(business.labor.overtime_max_minutes.value)
    start = datetime.now(tz).date()
    days: list[WorkerDayView] = []
    rostered = {h.weekday for w in world.workers.values() for h in w.working_hours}
    cursor = start
    while len(days) < 5 and (cursor - start).days < 14:
        if cursor.weekday() in rostered:
            hours = worker.hours_for(cursor.weekday())
            if hours is None:
                days.append(WorkerDayView(date=cursor.isoformat(), day=f"{cursor:%a}", shift="off"))
            else:
                opens = datetime.combine(cursor, hours.start, tzinfo=tz)
                closes = datetime.combine(cursor, hours.end, tzinfo=tz)
                latest = closes + timedelta(minutes=overtime if worker.overtime_eligible else 0)
                reach = ""
                if worker.overtime_eligible and overtime:
                    reach = f"can stay to {clock(closes + timedelta(minutes=overtime))}"
                extension = world.extension_for(worker.id, cursor)
                days.append(
                    WorkerDayView(
                        date=cursor.isoformat(),
                        day=f"{cursor:%a}",
                        shift=clock_range(hours.start, hours.end),
                        reach=reach,
                        extended=(
                            f"agreed to stay to {clock(extension.astimezone(tz))}"
                            if extension
                            else ""
                        ),
                        available=world.is_worker_available(worker.id, opens, closes),
                        # Once the shift and any overtime reach are behind the clock
                        # there is nothing left to block out or bring back.
                        actionable=datetime.now(tz) < latest,
                    )
                )
        cursor += timedelta(days=1)
    return days


def _offer_arrival(job_id: str, world: WorldState, tz: tzinfo) -> str:
    """The promised start, for saying which evening the hours belong to."""
    job = world.jobs.get(job_id)
    if job is None or not job.windows:
        return ""
    return clock(job.windows[0].start.astimezone(tz))


def world_view(world: WorldState, business: BusinessParams, tz: tzinfo) -> WorldView:
    # The wall clock, not world.as_of. The fold clock is the LAST EVENT'S time, so a
    # board quiet since Tuesday would judge "available now" as of Tuesday - the same
    # stale-clock family as the resurrected cancellation. "Now" means now.
    today = datetime.now(tz)
    end_of_day = today.replace(hour=23, minute=59)
    weekday = today.weekday()
    head_id = world.committed_plan_id or ""

    return WorldView(
        as_of=world.as_of.astimezone(tz).isoformat(),
        workers=[
            WorkerView(
                id=w.id,
                name=w.name,
                certifications=sorted(c.value for c in w.certifications),
                shift=(clock_range(h.start, h.end) if (h := w.hours_for(weekday)) else "off"),
                available=world.is_worker_available(w.id, today, end_of_day),
                overtime_eligible=w.overtime_eligible,
                shift_start=(f"{h0.start:%H:%M}" if (h0 := w.hours_for(0)) else ""),
                shift_end=(f"{h0.end:%H:%M}" if h0 else ""),
                phone=w.phone,
                days=_worker_week(w, world, business, tz),
            )
            for w in sorted(world.workers.values(), key=lambda w: w.id)
        ],
        vans=[
            VanView(
                id=v.id,
                label=v.label,
                available=world.is_van_available(v.id, today, end_of_day),
                stock=dict(sorted(v.stock.items())),
            )
            for v in sorted(world.vans.values(), key=lambda v: v.id)
        ],
        jobs=[
            JobView(
                id=j.id,
                customer_name=j.customer_name,
                phone=j.phone,
                address=j.location.address,
                quoted_total=j.quoted_total,
                booking_note=j.booking_note,
                service_type=j.service_type.value,
                duration_minutes=j.estimated_duration_min,
                crew_size=j.crew_size,
                certifications=sorted(c.value for c in j.required_certifications),
                commitment_state=j.commitment_state.value,
                commitment_cost=j.commitment_cost,
                window=(_window_text(j, tz) if j.windows else "any time"),
                lat=j.location.lat,
                lon=j.location.lon,
                transcript=j.provenance.transcript,
            )
            for j in world.active_jobs()
        ],
        crew_asks=[
            CrewAskView(
                ask_id=ask.ask_id,
                customer=ask.customer_name,
                phone=ask.phone,
                day=f"{ask.on_date:%a %d %b}",
                until_label=clock(ask.until_time.astimezone(tz)),
                detail=ask.detail,
                transcript=ask.transcript,
                candidates=[
                    {"id": w, "name": world.workers[w].name}
                    for w in ask.candidate_ids
                    if w in world.workers
                ],
                extended=[
                    world.workers[w].name
                    for w in ask.candidate_ids
                    if w in world.workers and world.extension_for(w, ask.on_date) is not None
                ],
            )
            for ask in sorted(world.crew_asks.values(), key=lambda a: a.ask_id)
        ],
        overtime_offers=[
            OvertimeOfferView(
                job_id=offer.job_id,
                customer=(job.customer_name if (job := world.jobs.get(offer.job_id)) else ""),
                day=f"{offer.on_date:%a %d %b}",
                arrival=_offer_arrival(offer.job_id, world, tz),
                overtime_minutes=offer.overtime_minutes,
                status=offer.status(today),
                offered_to=[world.workers[w].name for w in offer.offered_to if w in world.workers],
                offered_ids=list(offer.offered_to),
                claimed_by=(
                    world.workers[offer.claimed_by].name
                    if offer.claimed_by and offer.claimed_by in world.workers
                    else ""
                ),
                fallback=(
                    world.workers[offer.fallback].name
                    if offer.fallback in world.workers
                    else offer.fallback
                ),
                deadline=clock(offer.claim_deadline.astimezone(tz)),
            )
            for offer in sorted(world.overtime_offers.values(), key=lambda o: o.job_id)
            if (job := world.jobs.get(offer.job_id)) is None or job.is_active
        ],
        depot_address=next(
            (van.home_depot.address for van in world.vans.values() if van.home_depot.address), ""
        ),
        committed_plan_id=head_id,
        calibration_warning=calibration_banner(business) or "",
    )


def change_views(diff: PlanDiff) -> list[ChangeView]:
    return [
        ChangeView(
            job_id=c.job_id,
            customer_name=c.customer_name,
            kind=c.kind.value,
            description=c.describe(),
            needs_customer_call=c.customer_visible,
        )
        for c in diff.changes
    ]


def candidate_view(
    candidate: RepairCandidate, decision: AutonomyDecision, recommended: bool
) -> CandidateView:
    return CandidateView(
        strategy=candidate.strategy.name,
        description=candidate.strategy.description,
        jobs_served=candidate.jobs_served,
        changes=candidate.changes,
        customer_calls=candidate.customer_calls,
        blast_radius=candidate.diff.blast_radius.value,
        autonomy=decision.decision.value,
        autonomy_reasons=list(decision.reasons),
        diff=change_views(candidate.diff),
        recommended=recommended,
    )


def repair_options_summary(options: RepairOptions) -> list[RepairCandidate]:
    return list(options.candidates)
