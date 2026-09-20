"""Turning domain objects into what the board draws.

Kept apart from the endpoints so the mapping is testable without HTTP, and apart from
the domain so no view concern - minutes from midnight, hex colours, display names -
leaks into the model the solver reasons about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from glass_guru.api.models import (
    CandidateView,
    ChangeView,
    CostView,
    JobView,
    PlanView,
    RouteView,
    StopView,
    UnservedView,
    VanView,
    WorkerView,
    WorldView,
)
from glass_guru.config import BusinessParams, calibration_banner
from glass_guru.domain.autonomy import AutonomyDecision
from glass_guru.domain.diff import PlanDiff
from glass_guru.domain.enums import NOT_A_FAILURE
from glass_guru.domain.invariants import Violation
from glass_guru.domain.models import CostBreakdown, CrewRoute, PlanVersion
from glass_guru.domain.state import WorldState
from glass_guru.scheduler.costing import RouteCost
from glass_guru.scheduler.repair import RepairCandidate, RepairOptions


def _minute_of_day(moment: datetime, tz: tzinfo) -> int:
    local = moment.astimezone(tz)
    return local.hour * 60 + local.minute


def route_view(
    route: CrewRoute, world: WorldState, cost: RouteCost | None, tz: tzinfo
) -> RouteView:
    stops: list[StopView] = []
    previous_departure: datetime | None = None

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
            )
        )

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
            travel_labor=round(cost.travel_labor, 2),
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


def world_view(world: WorldState, business: BusinessParams, tz: tzinfo) -> WorldView:
    today = world.as_of
    end_of_day = today + timedelta(hours=12)
    weekday = today.astimezone(tz).weekday()
    head_id = world.committed_plan_id or ""

    return WorldView(
        as_of=world.as_of.astimezone(tz).isoformat(),
        workers=[
            WorkerView(
                id=w.id,
                name=w.name,
                certifications=sorted(c.value for c in w.certifications),
                shift=(f"{h.start:%H:%M}-{h.end:%H:%M}" if (h := w.hours_for(weekday)) else "off"),
                available=world.is_worker_available(w.id, today, end_of_day),
                overtime_eligible=w.overtime_eligible,
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
                service_type=j.service_type.value,
                duration_minutes=j.estimated_duration_min,
                crew_size=j.crew_size,
                certifications=sorted(c.value for c in j.required_certifications),
                commitment_state=j.commitment_state.value,
                commitment_cost=j.commitment_cost,
                window=(
                    f"{j.windows[0].start.astimezone(tz):%a %H:%M}"
                    f"-{j.windows[0].end.astimezone(tz):%H:%M}"
                    f" {j.windows[0].hardness.value}"
                    if j.windows
                    else "any time"
                ),
                lat=j.location.lat,
                lon=j.location.lon,
            )
            for j in world.active_jobs()
        ],
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
