"""HTTP surface for the dispatch board.

A thin layer over :class:`~glass_guru.service.DispatchService`. Everything the board
can do, the CLI and the MCP tools can already do, which is the point of having put the
operations in one place: a dispatcher clicking a button and an agent calling a tool
take the same code path and cannot drift apart.

Solving endpoints are declared ``def`` rather than ``async def`` on purpose. CP-SAT is
CPU-bound and would block the event loop for the duration of a solve, freezing the
board and every open event stream; FastAPI runs sync handlers in a worker thread
instead. Only the stream itself is async, because that is the one thing here that is
genuinely waiting rather than working.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from glass_guru.api import views
from glass_guru.api.models import (
    AcceptRequest,
    EventRequest,
    IntakeView,
    MessageView,
    ParamView,
    PlanView,
    RepairView,
    SlotView,
    TextRequest,
    TriageView,
    WorldView,
)
from glass_guru.cli.events import EventArgumentError, build_event
from glass_guru.config import BusinessParams
from glass_guru.obs.correlation import dispatch, new_dispatch_id
from glass_guru.obs.tracing import configure, span
from glass_guru.persistence.log import Workspace
from glass_guru.scheduler.travel.factory import TravelMode
from glass_guru.service import DispatchService, ServiceError

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


class Broadcaster:
    """Fans state changes out to every open board.

    Slow subscribers are dropped rather than allowed to apply back-pressure. A browser
    tab someone left open on a sleeping laptop must not be able to stall a solve.
    """

    def __init__(self, max_queue: int = 32) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._max_queue = max_queue

    def publish(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        message = json.dumps({"type": kind, **(payload or {})})
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        try:
            yield 'data: {"type": "connected"}\n\n'
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    # Keeps proxies from closing an idle connection.
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {message}\n\n"
        finally:
            self._subscribers.discard(queue)


broadcaster = Broadcaster()
app = FastAPI(title="glass-guru", version="0.1.0")

# The board runs on Vite's dev server during development and is served from this app
# in production, so cross-origin is a development-only concern.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def service() -> DispatchService:
    workspace = Workspace(os.environ.get("GLASS_GURU_WORKSPACE", ".glass-guru"))
    mode = TravelMode(os.environ.get("GLASS_GURU_TRAVEL", TravelMode.AUTO.value))
    return DispatchService(workspace, BusinessParams.load(), mode)


def _fail(exc: Exception, status: int = 400, remedy: str = "") -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": type(exc).__name__, "detail": str(exc), "remedy": remedy},
    )


# --------------------------------------------------------------------------- state


@app.get("/api/world", response_model=WorldView)
def get_world() -> WorldView:
    svc = service()
    try:
        return views.world_view(svc.world(), svc.business, svc.tz)
    except ServiceError as exc:
        raise _fail(exc, 409, "run `glass-guru init` to create a workspace") from exc


@app.get("/api/plan", response_model=PlanView | None)
def get_plan() -> PlanView | None:
    """The committed plan, or null when nothing is committed yet."""
    svc = service()
    try:
        world = svc.world()
    except ServiceError as exc:
        raise _fail(exc, 409) from exc

    head = svc.head()
    if head is None:
        return None

    from glass_guru.domain.invariants import ValidationConfig, validate_plan
    from glass_guru.scheduler.costing import cost_plan

    travel = svc.travel(world)
    violations = validate_plan(head, world, travel, ValidationConfig(business_tz=svc.tz))
    cost, route_costs = cost_plan(head, world, svc.business, svc.tz, head.unserved)
    return views.plan_view(head, world, cost, route_costs, violations, svc.tz)


@app.get("/api/params", response_model=list[ParamView])
def get_params() -> list[ParamView]:
    business = BusinessParams.load()
    return [
        ParamView(path=path, value=param.value, source=param.source.value, note=param.note)
        for path, param in business.walk()
    ]


# ------------------------------------------------------------------------ planning


@app.post("/api/plan/commit", response_model=PlanView)
def commit_plan(start_date: str | None = Query(default=None)) -> PlanView:
    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.commit"):
        try:
            world = svc.world()
            start = date.fromisoformat(start_date) if start_date else _default_start(svc)
            result = svc.plan_week(start, world=world)
            if result.feasible:
                head = svc.head()
                svc.commit(result.plan, expected_parent=head.id if head else None)
                broadcaster.publish("plan", {"plan_id": result.plan.id})
        except (ServiceError, ValueError) as exc:
            raise _fail(exc, 409) from exc

        return views.plan_view(
            result.plan,
            world,
            result.cost,
            list(result.route_costs),
            result.violations,
            svc.tz,
        )


@app.post("/api/repair", response_model=RepairView)
def repair() -> RepairView:
    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.repair"):
        try:
            options, baseline = svc.repair()
        except ServiceError as exc:
            raise _fail(exc, 409, "commit a plan before repairing one") from exc

        best = options.best_by_fewest_calls
        return RepairView(
            baseline_plan_id=baseline.id,
            candidates=[
                views.candidate_view(c, svc.autonomy(c.diff), c is best) for c in options.candidates
            ],
            recommended=best.strategy.name if best else "",
            rationale=(
                f"{best.jobs_served} served, {best.customer_calls} call(s), "
                f"{best.changes} change(s)"
                if best
                else ""
            ),
        )


@app.post("/api/repair/apply", response_model=PlanView)
def apply_repair(strategy: str = Query(...), force: bool = Query(default=False)) -> PlanView:
    """Commit one repair candidate.

    ``force`` is what a dispatcher's approval looks like over HTTP. Without it the
    deterministic autonomy policy refuses anything customer-visible - the endpoint
    cannot be talked past, only overridden by a person who saw the diff.
    """
    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.apply_repair", strategy=strategy):
        try:
            options, baseline = svc.repair()
        except ServiceError as exc:
            raise _fail(exc, 409) from exc

        candidate = next((c for c in options.candidates if c.strategy.name == strategy), None)
        if candidate is None:
            raise HTTPException(404, detail={"error": "NoSuchStrategy", "detail": strategy})

        decision = svc.autonomy(candidate.diff)
        if not decision.auto and not force:
            raise HTTPException(
                status_code=412,
                detail={
                    "error": "NeedsApproval",
                    "detail": decision.explain(),
                    "remedy": "a dispatcher must review this diff, then retry with force",
                },
            )

        world = svc.world()
        from glass_guru.domain.invariants import ValidationConfig, validate_plan
        from glass_guru.scheduler.costing import cost_plan

        travel = svc.travel(world)
        violations = validate_plan(
            candidate.plan, world, travel, ValidationConfig(business_tz=svc.tz)
        )
        if violations:
            # A plan that fails its own invariants never reaches storage, whoever asked.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Infeasible",
                    "detail": "; ".join(str(v) for v in violations[:3]),
                },
            )

        try:
            svc.commit(candidate.plan, expected_parent=baseline.id)
        except ServiceError as exc:
            raise _fail(exc, 409) from exc

        broadcaster.publish("plan", {"plan_id": candidate.plan.id, "strategy": strategy})
        cost, route_costs = cost_plan(
            candidate.plan, world, svc.business, svc.tz, candidate.plan.unserved
        )
        return views.plan_view(candidate.plan, world, cost, route_costs, (), svc.tz)


# -------------------------------------------------------------------------- events


@app.post("/api/events")
def record_event(request: EventRequest) -> dict[str, str]:
    svc = service()
    with dispatch(new_dispatch_id("web")) as dispatch_id, span("api.record_event"):
        try:
            at = _clock(svc, request.at, time(8, 0))
            assert at is not None  # a fallback is always supplied
            event = build_event(
                request.kind,
                request.target or None,
                at=at,
                dispatch_id=dispatch_id,
                until=_clock(svc, request.until, None),
                window_start=_clock(svc, request.window_start, None),
                window_end=_clock(svc, request.window_end, None),
                minutes=request.minutes,
                multiplier=request.multiplier,
                commitment_cost=request.commitment_cost,
                reason=request.reason,
            )
            svc.apply_events([event])
        except (EventArgumentError, ServiceError, ValueError) as exc:
            raise _fail(exc, 400, "check the event kind and target id") from exc

        broadcaster.publish("world", {"event": event.type})
        return {"event_id": event.event_id, "type": event.type, "dispatch_id": dispatch_id}


# -------------------------------------------------------------------------- agents


@app.post("/api/triage", response_model=TriageView)
def run_triage(request: TextRequest) -> TriageView:
    from glass_guru.agents.a2a.types import submitted
    from glass_guru.agents.llm.factory import build_llm
    from glass_guru.agents.registry import TRIAGE_SKILL, TriageContext, build_transport

    svc = service()
    with dispatch(new_dispatch_id("web")) as dispatch_id, span("api.triage"):
        try:
            world = svc.world()
        except ServiceError as exc:
            raise _fail(exc, 409) from exc

        context = TriageContext(world=world, on_date=_default_start(svc), tz=svc.tz)
        transport = build_transport(build_llm(), triage_context=context)
        task = transport.send(
            transport.discover(TRIAGE_SKILL),
            submitted(request.text, dispatch_id=dispatch_id),
        )
        data = artifact.data if (artifact := task.artifact("events")) else {}

        return TriageView(
            state=task.state.value,
            summary=str(data.get("summary", "")),
            events=list(data.get("events", [])),
            question=(
                task.status.message.text_content if task.needs_input and task.status.message else ""
            ),
            unknown_targets=list(data.get("unknown_targets", [])),
            rejected=list(data.get("rejected", [])),
            repairs=int(data.get("repairs", 0)),
        )


@app.post("/api/triage/accept")
def accept_triage(request: AcceptRequest) -> dict[str, int]:
    """Record events a dispatcher reviewed. Nothing an agent produced is stored until
    a person has seen it."""
    from pydantic import TypeAdapter

    from glass_guru.domain.events import Event

    svc = service()
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    with dispatch(new_dispatch_id("web")), span("api.accept_triage"):
        try:
            events = [adapter.validate_python(e) for e in request.events]
            svc.apply_events(events)
        except (ServiceError, ValueError) as exc:
            raise _fail(exc, 400) from exc
        broadcaster.publish("world", {"accepted": len(events)})
        return {"recorded": len(events)}


@app.post("/api/intake", response_model=IntakeView)
def run_intake(request: TextRequest) -> IntakeView:
    from glass_guru.agents.intake import intake
    from glass_guru.agents.llm.factory import build_llm
    from glass_guru.api.models import DraftView

    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.intake"):
        try:
            world = svc.world()
        except ServiceError as exc:
            raise _fail(exc, 409) from exc

        result = intake(
            build_llm(),
            request.text,
            business=svc.business,
            now=world.as_of,
        )
        call = result.call
        draft = DraftView(
            customer_name=call.customer_name if call else "",
            phone=call.phone if call else "",
            address=call.address if call else "",
            service_type=call.service_type if call else "",
            duration_minutes=result.estimate.duration_min if result.estimate else 0,
            duration_confidence=result.estimate.confidence_min if result.estimate else 0,
            crew_size=result.estimate.crew_size if result.estimate else 0,
            certifications=(
                sorted(c.value for c in result.estimate.required_certifications)
                if result.estimate
                else []
            ),
            commitment_cost=result.commitment_cost,
            commitment_quotes=list(result.commitment_quotes),
            lead_time_days=result.estimate.lead_time_days if result.estimate else 0,
            site_notes=call.site_notes if call else "",
            lat=result.draft.location.lat if result.draft else None,
            lon=result.draft.location.lon if result.draft else None,
        )

        slots: list[SlotView] = []
        if result.bookable and result.draft is not None:
            options = svc.booking_slots(result.draft, _default_start(svc))
            slots = [
                SlotView(
                    date=s.on_date.isoformat(),
                    window=(
                        f"{s.quoted_window.start.astimezone(svc.tz):%a %H:%M}"
                        f"-{s.quoted_window.end.astimezone(svc.tz):%H:%M}"
                    ),
                    marginal_cost=round(s.marginal_cost, 2),
                    crew=" + ".join(s.worker_names),
                    reason=s.reason,
                )
                for s in options.slots
            ]

        return IntakeView(
            draft=draft,
            bookable=result.bookable,
            missing=list(result.missing_required),
            ask_next=list(result.ask_next),
            slots=slots,
            repairs=result.extraction.repairs,
            note=result.geocode_note,
        )


@app.post("/api/comms", response_model=list[MessageView])
def draft_messages(strategy: str = Query(...)) -> list[MessageView]:
    """Draft customer messages for one repair candidate, with grounding already checked."""
    from glass_guru.agents.comms import draft_customer_messages
    from glass_guru.agents.llm.factory import build_llm

    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.comms", strategy=strategy):
        try:
            options, _ = svc.repair()
        except ServiceError as exc:
            raise _fail(exc, 409) from exc

        candidate = next((c for c in options.candidates if c.strategy.name == strategy), None)
        if candidate is None:
            raise HTTPException(404, detail={"error": "NoSuchStrategy", "detail": strategy})

        result = draft_customer_messages(
            build_llm(),
            candidate.diff,
            tz=svc.tz,
            reason=f"repair: {candidate.strategy.description}",
        )
        by_job: dict[str, list[str]] = {}
        for issue in result.issues:
            by_job.setdefault(issue.job_id, []).append(f"{issue.phrase}: {issue.detail}")

        return [
            MessageView(
                job_id=d.job_id,
                channel=d.channel,
                body=d.body,
                grounded=not by_job.get(d.job_id),
                issues=by_job.get(d.job_id, []),
            )
            for d in result.drafts
        ]


# --------------------------------------------------------------------------- diffs


@app.get("/api/diff")
def get_diff(before: str = Query(...), after: str = Query(...)) -> dict[str, Any]:
    svc = service()
    try:
        diff = svc.diff(before, after)
    except ServiceError as exc:
        raise _fail(exc, 404) from exc
    return {
        "summary": diff.summary(),
        "blast_radius": diff.blast_radius.value,
        "changes": [c.model_dump() for c in views.change_views(diff)],
    }


@app.get("/api/history")
def get_history() -> list[dict[str, Any]]:
    svc = service()
    return [
        {
            "plan_id": plan.id,
            "content_hash": plan.content_hash,
            "created_at": plan.created_at.astimezone(svc.tz).isoformat(),
            "label": plan.label,
            "jobs": sum(len(r.stops) for r in plan.routes),
        }
        for plan in svc.workspace.plans.ancestry()
    ]


# -------------------------------------------------------------------------- stream


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    """Server-sent events, so a board reflects a change made from the CLI or an agent."""
    return StreamingResponse(
        broadcaster.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------------- helpers


def _default_start(svc: DispatchService) -> date:
    """The horizon start: whatever is committed, else the fixture's week."""
    head = svc.head()
    if head is not None:
        return head.horizon_start
    from glass_guru.fixtures.sample_business import WEEK_START

    return WEEK_START


def _clock(svc: DispatchService, value: Any, fallback: time | None) -> datetime | None:
    if not value and fallback is None:
        return None
    parsed = datetime.strptime(str(value), "%H:%M").time() if value else fallback
    if parsed is None:
        return None
    return datetime.combine(_default_start(svc), parsed, tzinfo=svc.tz)


# --------------------------------------------------------------------- static site


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        """Serve the built board, letting the client router own every other path."""
        candidate = WEB_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")


def main() -> None:
    import uvicorn

    configure(service="glass-guru-api")
    uvicorn.run(app, host="127.0.0.1", port=8000)


with suppress(ImportError):  # pragma: no cover - only for `python -m`
    pass
