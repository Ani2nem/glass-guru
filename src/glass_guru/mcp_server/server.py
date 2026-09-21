"""MCP tools over the scheduling engine.

This is the boundary between agents and the deterministic core, and the place the
project's central claim gets enforced rather than merely stated. An agent can ask for
a plan, price a job, or record what someone said on the phone. It cannot assert an
arrival time, declare a plan feasible, or commit something that fails its invariants -
those paths simply do not exist here.

Three things every tool does:

* **Returns a small, closed, typed result.** Nova Lite has to read these; every field
  it must skip is a chance to misread the one that matters.
* **Pre-computes the explanation.** Why a job is unserved is arithmetic, and
  arithmetic stays on the deterministic side. An agent that had to infer it would
  eventually infer it wrong and say so confidently.
* **Runs under a dispatch id.** A whole episode - a sentence, three tool calls, two
  solves, a commit - joins up as one trace.

Errors come back as structured values rather than exceptions. A model handed a stack
trace will paraphrase it into something plausible and wrong; a model handed
``{"error": ..., "remedy": ...}`` has something to act on.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import date, datetime, time, tzinfo
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from glass_guru.cli.events import EventArgumentError, build_event
from glass_guru.config import BusinessParams
from glass_guru.domain.diff import PlanDiff
from glass_guru.domain.enums import Certification, ServiceType
from glass_guru.domain.models import Job, Location, TimeWindow, UnservedJob
from glass_guru.domain.state import WorldState
from glass_guru.fixtures.sample_business import WEEK_START
from glass_guru.geocoding import GeocodeError, for_service_area
from glass_guru.mcp_server.models import (
    BookingSummary,
    ChangeSummary,
    DiffSummary,
    EventAck,
    JobSummary,
    PlanSummary,
    RepairCandidateSummary,
    RepairSummary,
    SlotSummary,
    ToolError,
    UnservedSummary,
    VanSummary,
    WorkerSummary,
    WorldSummary,
)
from glass_guru.obs.correlation import dispatch
from glass_guru.obs.tracing import configure, record_error, span
from glass_guru.persistence.log import Workspace
from glass_guru.scheduler.travel.cache import CacheMiss
from glass_guru.scheduler.travel.factory import TravelMode
from glass_guru.scheduler.travel.osrm import OsrmUnavailable
from glass_guru.service import DispatchService, ServiceError

mcp = MCPServer(
    name="glass-guru",
    instructions=(
        "Scheduling engine for a glass and window service business. The engine owns "
        "every checkable fact: arrival times, feasibility, certifications, crew sizes, "
        "van stock, travel and cost. Never assert any of those yourself - ask for them. "
        "Use suggest_booking_slots to answer 'when can we come out', repair_plan after "
        "a disruption, and record_event to log what a caller actually said. Every tool "
        "returns a small typed result with the explanation already computed."
    ),
)


def _service() -> DispatchService:
    workspace = Workspace(os.environ.get("GLASS_GURU_WORKSPACE", ".glass-guru"))
    travel_mode = TravelMode(os.environ.get("GLASS_GURU_TRAVEL", TravelMode.AUTO.value))
    return DispatchService(workspace, BusinessParams.load(), travel_mode)


def _fail(exc: Exception, remedy: str = "") -> ToolError:
    record_error(exc)
    return ToolError(error=type(exc).__name__, detail=str(exc), remedy=remedy)


def _window_text(job: Job, tz: tzinfo) -> str:
    if not job.windows:
        return "any time"
    window = job.windows[0]
    return (
        f"{window.start.astimezone(tz):%a %H:%M}-{window.end.astimezone(tz):%H:%M}"
        f" ({window.hardness.value})"
    )


def _changes(diff: PlanDiff) -> list[ChangeSummary]:
    return [
        ChangeSummary(
            job_id=change.job_id,
            customer_name=change.customer_name,
            kind=change.kind.value,
            description=change.describe(),
            needs_customer_call=change.customer_visible,
        )
        for change in diff.changes
    ]


def _unserved(world: WorldState, items: Sequence[UnservedJob]) -> list[UnservedSummary]:
    out: list[UnservedSummary] = []
    for item in items:
        job = world.jobs.get(item.job_id)
        out.append(
            UnservedSummary(
                job_id=item.job_id,
                customer_name=job.customer_name if job else "",
                reason=item.reason.value,
                detail=item.detail,
            )
        )
    return out


# --------------------------------------------------------------------------- tools


@mcp.tool(
    description=(
        "Who is working, which vans are running, and what jobs are outstanding. "
        "Start here when you need to know what is possible before proposing anything."
    )
)
def get_world_state() -> WorldSummary | ToolError:
    with dispatch(), span("mcp.get_world_state"):
        try:
            service = _service()
            world = service.world()
        except ServiceError as exc:
            return _fail(exc, "run `glass-guru init` to create a workspace")

        tz = service.tz
        today = world.as_of
        end_of_day = datetime.combine(today.date(), time(23, 59), tzinfo=tz)
        head = service.head()

        return WorldSummary(
            as_of=world.as_of.astimezone(tz).isoformat(),
            workers=[
                WorkerSummary(
                    id=w.id,
                    name=w.name,
                    certifications=sorted(c.value for c in w.certifications),
                    available_today=world.is_worker_available(w.id, today, end_of_day),
                    shift=(
                        f"{h.start}-{h.end}"
                        if (h := w.hours_for(today.astimezone(tz).weekday()))
                        else "not working"
                    ),
                )
                for w in sorted(world.workers.values(), key=lambda w: w.id)
            ],
            vans=[
                VanSummary(
                    id=v.id,
                    label=v.label,
                    available_today=world.is_van_available(v.id, today, end_of_day),
                    stock=dict(sorted(v.stock.items())),
                )
                for v in sorted(world.vans.values(), key=lambda v: v.id)
            ],
            jobs=[
                JobSummary(
                    id=j.id,
                    customer_name=j.customer_name,
                    service_type=j.service_type.value,
                    duration_minutes=j.estimated_duration_min,
                    crew_size=j.crew_size,
                    required_certifications=sorted(c.value for c in j.required_certifications),
                    commitment_state=j.commitment_state.value,
                    window=_window_text(j, tz),
                    scheduled=(
                        f"{found[0].date.isoformat()} {found[1].arrival.astimezone(tz):%H:%M}"
                        if head and (found := head.stop_for(j.id))
                        else ""
                    ),
                )
                for j in world.active_jobs()
            ],
            committed_plan_id=head.id if head else "",
            notes=[
                "Costs are built on business parameters that are still estimates.",
            ],
        )


@mcp.tool(
    description=(
        "When can we come out, and what does each option cost? Prices a prospective "
        "job into every day of the horizon by marginal insertion cost. Use this to "
        "offer slots on a call - the cheapest option is often many times cheaper than "
        "the dearest, and that is a real saving, not a preference."
    )
)
def suggest_booking_slots(
    service_type: Annotated[str, Field(description="Service type from the catalog")],
    duration_minutes: Annotated[int, Field(ge=15, le=600)],
    address: Annotated[str, Field(description="Street address, geocoded if unknown")] = "",
    latitude: Annotated[float | None, Field(ge=-90, le=90)] = None,
    longitude: Annotated[float | None, Field(ge=-180, le=180)] = None,
    required_certifications: list[str] | None = None,
    crew_size: Annotated[int, Field(ge=1, le=2)] = 1,
    revenue: float = 0.0,
) -> BookingSummary | ToolError:
    with dispatch(), span("mcp.suggest_booking_slots", service=service_type):
        try:
            svc = _service()
            if latitude is not None and longitude is not None:
                location = Location(lat=latitude, lon=longitude, address=address)
            elif address:
                location = for_service_area().geocode(address)
            else:
                return ToolError(
                    error="MissingLocation",
                    detail="a booking needs a place to go",
                    remedy="pass address, or both latitude and longitude",
                )

            world = svc.world()
            horizon_days = svc.horizon_params().days
            draft = Job(
                id="draft",
                customer_id="draft",
                customer_name="prospective",
                location=location,
                service_type=ServiceType(service_type),
                required_certifications=frozenset(
                    Certification(c) for c in (required_certifications or [])
                ),
                crew_size=crew_size,
                estimated_duration_min=duration_minutes,
                revenue=revenue,
                windows=tuple(
                    TimeWindow(
                        start=datetime.combine(
                            date.fromordinal(WEEK_START.toordinal() + offset),
                            time(8, 0),
                            tzinfo=svc.tz,
                        ),
                        end=datetime.combine(
                            date.fromordinal(WEEK_START.toordinal() + offset),
                            time(17, 0),
                            tzinfo=svc.tz,
                        ),
                    )
                    for offset in range(horizon_days)
                ),
                requested_at=world.as_of,
            )
            options = svc.booking_slots(draft, WEEK_START)
        except CacheMiss as exc:
            # A brand-new address is exactly what a frozen snapshot cannot cover.
            # Say so, rather than crashing or quietly substituting a straight line.
            return _fail(
                exc,
                "no cached travel data for this address. Set "
                "GLASS_GURU_TRAVEL=warm to compute the few missing legs from "
                "OSRM, or quote a location already on the books",
            )
        except OsrmUnavailable as exc:
            return _fail(exc, "start the routing backend: docker compose up -d osrm")
        except (ServiceError, GeocodeError, ValueError) as exc:
            return _fail(exc, "check the service type, certifications and address")

        return BookingSummary(
            slots=[
                SlotSummary(
                    date=slot.on_date.isoformat(),
                    window=(
                        f"{slot.quoted_window.start.astimezone(svc.tz):%a %H:%M}"
                        f"-{slot.quoted_window.end.astimezone(svc.tz):%H:%M}"
                    ),
                    marginal_cost=round(slot.marginal_cost, 2),
                    crew=" + ".join(slot.worker_names),
                    reason=slot.reason,
                )
                for slot in options.slots
            ],
            unavailable=[
                UnservedSummary(
                    job_id="draft",
                    customer_name="prospective",
                    reason=item.reason.value,
                    detail=f"{item.on_date.isoformat()}: {item.detail}",
                )
                for item in options.unavailable
            ],
            days_considered=options.evaluated_days,
            savings_vs_worst=round(options.savings_vs_worst, 2),
        )


@mcp.tool(
    description=(
        "Plan the rolling horizon. Returns what the plan achieved and whether it "
        "passes every invariant. Set commit=true to make it the live plan; an "
        "infeasible plan is never stored."
    )
)
def plan_week(
    start_date: Annotated[str, Field(description="ISO date, e.g. 2026-09-21")] = "",
    commit: bool = False,
) -> PlanSummary | ToolError:
    with dispatch(), span("mcp.plan_week", commit=commit):
        try:
            svc = _service()
            start = date.fromisoformat(start_date) if start_date else WEEK_START
            world = svc.world()
            result = svc.plan_week(start, world=world)
            committed = False
            if commit and result.feasible:
                head = svc.head()
                svc.commit(result.plan, expected_parent=head.id if head else None)
                committed = True
        except (CacheMiss, OsrmUnavailable) as exc:
            return _fail(exc, "travel data is unavailable for a scheduled address")
        except (ServiceError, ValueError) as exc:
            return _fail(exc, "check the date, or run `glass-guru init` first")

        return PlanSummary(
            plan_id=result.plan.id,
            content_hash=result.plan.content_hash,
            horizon_start=result.plan.horizon_start.isoformat(),
            horizon_end=result.plan.horizon_end.isoformat(),
            jobs_scheduled=len(result.horizon.scheduled_job_ids),
            jobs_unserved=len(result.horizon.unserved),
            crews_used=len(result.plan.routes),
            total_cost=round(result.cost.total, 2),
            feasible=result.feasible,
            violations=[str(v) for v in result.violations],
            unserved=_unserved(world, result.horizon.unserved),
            committed=committed,
        )


@mcp.tool(
    description=(
        "Repair the committed plan after a disruption. Returns several labelled "
        "trade-offs - keep every promise and serve less, or serve more and make phone "
        "calls - each with how many customers would need telling and whether it may be "
        "applied without a dispatcher. Choosing between them is a judgement; pricing "
        "them is not."
    )
)
def repair_plan() -> RepairSummary | ToolError:
    with dispatch(), span("mcp.repair_plan"):
        try:
            svc = _service()
            options, baseline = svc.repair()
        except (CacheMiss, OsrmUnavailable) as exc:
            return _fail(exc, "travel data is unavailable for a scheduled address")
        except ServiceError as exc:
            return _fail(exc, "commit a plan before repairing one")

        best = options.best_by_fewest_calls
        candidates: list[RepairCandidateSummary] = []
        for candidate in options.candidates:
            decision = svc.autonomy(candidate.diff)
            candidates.append(
                RepairCandidateSummary(
                    strategy=candidate.strategy.name,
                    description=candidate.strategy.description,
                    jobs_served=candidate.jobs_served,
                    changes=candidate.changes,
                    customer_calls=candidate.customer_calls,
                    blast_radius=candidate.diff.blast_radius.value,
                    autonomy=decision.decision.value,
                    autonomy_reasons=list(decision.reasons),
                    diff=_changes(candidate.diff),
                )
            )

        return RepairSummary(
            baseline_plan_id=baseline.id,
            candidates=candidates,
            recommended=best.strategy.name if best else "",
            recommendation_reason=(
                f"{best.jobs_served} jobs served with {best.customer_calls} customer "
                f"call(s) and {best.changes} change(s)"
                if best
                else "no candidate available"
            ),
        )


@mcp.tool(
    description=(
        "Record something that happened: a van breaking down, a worker calling in "
        "sick, a customer confirming a window, a job running over. This is the only "
        "way world state changes. Use the exact ids from get_world_state."
    )
)
def record_event(
    kind: Annotated[str, Field(description="e.g. van-unavailable, job-confirmed")],
    target: Annotated[str, Field(description="van, worker or job id")] = "",
    at: Annotated[str, Field(description="HH:MM, defaults to 08:00")] = "",
    until: str = "",
    window_start: str = "",
    window_end: str = "",
    minutes: int | None = None,
    multiplier: float | None = None,
    commitment_cost: Annotated[
        float,
        Field(
            description=(
                "What it would cost to break this promise. Raise it when the customer "
                "has arranged their day around the slot."
            )
        ),
    ] = 0.0,
    reason: str = "",
) -> EventAck | ToolError:
    with dispatch() as dispatch_id, span("mcp.record_event", kind=kind):
        try:
            svc = _service()
            tz = svc.tz

            def moment(clock: str, fallback: time) -> datetime:
                parsed = datetime.strptime(clock, "%H:%M").time() if clock else fallback
                return datetime.combine(WEEK_START, parsed, tzinfo=tz)

            event = build_event(
                kind,
                target or None,
                at=moment(at, time(8, 0)),
                dispatch_id=dispatch_id,
                until=moment(until, time(17, 0)) if until else None,
                window_start=moment(window_start, time(8, 0)) if window_start else None,
                window_end=moment(window_end, time(17, 0)) if window_end else None,
                minutes=minutes,
                multiplier=multiplier,
                commitment_cost=commitment_cost,
                reason=reason,
            )
            svc.apply_events([event])
        except (EventArgumentError, ServiceError, ValueError) as exc:
            return _fail(exc, "check the event kind and that the target id exists")

        return EventAck(
            recorded=True,
            event_type=event.type,
            event_id=event.event_id,
            dispatch_id=dispatch_id,
            message=f"recorded {event.type}",
        )


@mcp.tool(
    description=(
        "What changed between two plan versions, and whether any customer would "
        "notice. A promise is a window, so a crew arriving later within the window "
        "the customer was given is not a change they experience."
    )
)
def get_plan_diff(before_plan_id: str, after_plan_id: str) -> DiffSummary | ToolError:
    with dispatch(), span("mcp.get_plan_diff"):
        try:
            svc = _service()
            diff = svc.diff(before_plan_id, after_plan_id)
        except ServiceError as exc:
            return _fail(exc, "list versions with `glass-guru history`")

        return DiffSummary(
            before_plan_id=before_plan_id,
            after_plan_id=after_plan_id,
            summary=diff.summary(),
            blast_radius=diff.blast_radius.value,
            changes=_changes(diff),
        )


def main() -> None:
    """Run the server over stdio. HTTP transport arrives with deployment."""
    configure(service="glass-guru-mcp")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
