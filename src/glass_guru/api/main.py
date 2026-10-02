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
import secrets
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from glass_guru.api import views
from glass_guru.api.models import (
    AcceptRequest,
    BookRequest,
    EventRequest,
    IntakeView,
    MessageView,
    NoteView,
    ParamView,
    PlanView,
    RepairView,
    SlotView,
    TextRequest,
    TriageView,
    UnavailableDayView,
    WorldView,
)
from glass_guru.cli.events import EventArgumentError, build_event
from glass_guru.config import BusinessParams
from glass_guru.domain.models import Job
from glass_guru.domain.state import WorldState
from glass_guru.formatting import clock, clock_range
from glass_guru.geocoding import GeocodeError, OutsideServiceArea
from glass_guru.obs.correlation import dispatch, new_dispatch_id
from glass_guru.obs.tracing import configure, span
from glass_guru.persistence.log import PlanConflict, Workspace
from glass_guru.scheduler.booking import SlotSuggestion
from glass_guru.scheduler.pricing import quote_for
from glass_guru.scheduler.travel.cache import CacheMiss
from glass_guru.scheduler.travel.factory import TravelMode, build_travel
from glass_guru.service import DispatchService, ServiceError

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"

#: A weekday mid-morning. The snapshot is keyed by day type and hour bucket, so probing
#: at wall-clock "now" asks for a weekend leg every Saturday and reports a healthy
#: container as degraded. The business does not run weekends; the probe should not
#: pretend otherwise.
_PROBE_AT = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)


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


# --------------------------------------------------------------------------- health


# --------------------------------------------------------------------- failures
#
# Registered once for the whole app rather than caught per route. Both of these arise
# several layers below the endpoint - inside the solver, inside the travel cache - and
# a route that forgot to catch one returned "Internal Server Error" to a dispatcher for
# something with a perfectly good explanation.


@app.exception_handler(OutsideServiceArea)
def _outside_service_area(_: Request, exc: OutsideServiceArea) -> JSONResponse:
    """Not a failure to understand the address. A business answer."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "OutsideServiceArea",
            "detail": str(exc),
            "remedy": "check the address, or book it as an out-of-area job deliberately",
        },
    )


@app.exception_handler(GeocodeError)
def _geocode_failed(_: Request, exc: GeocodeError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "GeocodeError",
            "detail": str(exc),
            "remedy": "add a street number, or a city, and try again",
        },
    )


@app.exception_handler(CacheMiss)
def _travel_cache_miss(_: Request, exc: CacheMiss) -> JSONResponse:
    """A real address nobody has ever quoted before.

    The frozen snapshot covers the fixture's geography and refuses to invent a leg it
    does not have, which is right for tests and wrong mid-call. `warm` mode answers
    from the snapshot and asks OSRM for the rest.
    """
    return JSONResponse(
        status_code=409,
        content={
            "error": "CacheMiss",
            "detail": str(exc),
            "remedy": (
                "this address is not in the frozen travel snapshot. Start OSRM and run "
                "with live routing:  docker compose up -d osrm  then  "
                "GLASS_GURU_TRAVEL=warm make api"
            ),
        },
    )


# ------------------------------------------------------------------------- auth
#
# A function URL with AuthType NONE is reachable by anyone who has the URL, and until
# now nothing behind it asked who was calling. That is fine on a laptop and not fine
# for a business's schedule, which this can read, change and book against.
#
# A shared key rather than JWTs, deliberately: there is one dispatcher and no identity
# provider, and a signing key nobody rotates is worse than a shared secret somebody
# does. When there are users, this is the seam that becomes a real dependency.


def api_key() -> str:
    return os.environ.get("GLASS_GURU_API_KEY", "")


#: Reachable without a key. Health and readiness are how a load balancer and a deploy
#: decide whether this container works, and neither can hold a secret.
_OPEN_PATHS = frozenset({"/api/health", "/api/ready"})


@app.middleware("http")
async def require_api_key(request: Request, call_next: Any) -> Any:
    """Refuse unauthenticated calls when a key is configured.

    Unset means open, which is what makes `make dev` work with no setup. That default
    is only safe because the deployment refuses to be public without one - terraform
    will not apply a public function URL with no key, and /api/ready says which mode
    it is in, so an open deployment cannot go unnoticed.
    """
    expected = api_key()
    path = request.url.path
    if not expected or path in _OPEN_PATHS or not path.startswith("/api/"):
        return await call_next(request)

    presented = request.headers.get("x-api-key", "")
    if not presented:
        bearer = request.headers.get("authorization", "")
        presented = bearer[7:] if bearer.lower().startswith("bearer ") else ""

    # Constant time: a comparison that returns early leaks the key one character at a
    # time to anyone willing to measure.
    if not presented or not secrets.compare_digest(presented, expected):
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "detail": "this deployment requires an API key",
                "remedy": "send it as X-API-Key, or as Authorization: Bearer <key>",
            },
        )
    return await call_next(request)


def stream_mode() -> str:
    """``sse`` or ``poll``. How the board should find out that something changed.

    This is a cost decision, not a technical one. Server-sent events are the better
    mechanism and are free on a server that is running anyway. On Lambda the function
    is billed for as long as the stream is held open, so one board left open for a
    working day costs about $29 a month against about $0.58 for polling every ten
    seconds - and left open overnight, more than the always-on container it replaced.
    """
    return os.environ.get("GLASS_GURU_STREAM", "sse")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness. Deliberately does no work at all.

    A liveness probe that touches the solver restarts a healthy task whenever a solve
    is holding the worker threads, which turns a slow minute into an outage.

    It also tells the board how to watch for changes, because that is the one thing
    the client cannot work out for itself - a stream that is expensive still works,
    so there is no failure to fall back from.
    """
    return {"status": "ok", "version": app.version, "stream": stream_mode()}


@app.get("/api/ready")
def ready() -> JSONResponse:
    """Readiness. Can this container actually plan, or has it merely started?

    The failure worth catching is the process that is up and answering while missing
    something it needs, because that is the one a port check calls healthy. Each probe
    below stands for a file the image copies selectively and could stop copying:
    business parameters, the travel snapshot, the built board.

    Cheap on purpose - one parameter load and one travel leg, no solve - because a
    load balancer runs this every few seconds on every task.
    """
    checks: dict[str, str] = {}

    try:
        business = BusinessParams.load()
        checks["params"] = f"{sum(1 for _ in business.walk())} parameters"
    except Exception as exc:
        checks["params"] = f"FAILED: {exc}"
        business = None

    if business is not None:
        try:
            mode = TravelMode(os.environ.get("GLASS_GURU_TRAVEL", TravelMode.AUTO.value))
            provider = build_travel(business, mode)
            # The fixture's own depot and first stop. Hand-typed coordinates looked
            # equivalent and were not: the addresses are geocoded, so a literal from
            # the source landed in a different geohash cell than anything frozen.
            from glass_guru.fixtures.sample_business import DEPOT, PROBE_STOP

            leg = provider.leg(DEPOT, PROBE_STOP, _PROBE_AT)
            checks["travel"] = f"{mode.value}: depot leg {leg.minutes:.0f} min"
        except Exception as exc:
            checks["travel"] = f"FAILED: {type(exc).__name__}: {exc}"

    checks["board"] = "built" if WEB_DIST.exists() else "FAILED: no web/dist in the image"
    # Not a failure - running open is a legitimate local choice. It is reported so that
    # a deployment cannot be open without anybody being able to see that it is.
    checks["auth"] = "api key required" if api_key() else "open, no key configured"

    failed = {name: detail for name, detail in checks.items() if detail.startswith("FAILED")}
    return JSONResponse(
        status_code=503 if failed else 200,
        content={"status": "degraded" if failed else "ready", "checks": checks},
    )


# --------------------------------------------------------------------------- state


@app.get("/api/world", response_model=WorldView)
def get_world() -> WorldView:
    svc = service()
    try:
        return views.world_view(svc.world(), svc.business, svc.tz)
    except ServiceError as exc:
        raise _fail(exc, 409, "run `glass-guru init` to create a workspace") from exc


@app.get("/api/week")
def get_week() -> dict[str, str]:
    """The days the board should draw, whether or not anything is planned yet.

    An empty diary is not the same thing as no calendar. The board used to render
    nothing at all until a plan existed, so a business with no bookings saw a blank
    panel instead of an open week - which is exactly when you most want to see which
    days are free.
    """
    svc = service()
    try:
        start = _default_start(svc)
    except ServiceError:
        start = next_working_day_from_today()
    days = int(svc.business.horizon.days.value)
    return {"start": start.isoformat(), "end": (start + timedelta(days=days - 1)).isoformat()}


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
            candidate.plan,
            world,
            travel,
            # Promises this candidate breaks are authorised by the act of applying it;
            # `force` above is where a dispatcher agreed to that.
            ValidationConfig(
                business_tz=svc.tz,
                released_job_ids=frozenset(candidate.released_promises),
            ),
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
            at = _clock(svc, request.at, None, default_now=True)
            assert at is not None  # default_now always yields a moment
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


@app.post("/api/note", response_model=NoteView)
def read_note(request: TextRequest, kind: str | None = Query(default=None)) -> NoteView:
    """One box. Work out what the note is, then hand it to the right agent.

    ``kind`` overrides the classification, which is how the dispatcher corrects it
    without retyping. The override is the reason routing by model is safe here: the
    worst case costs one click, not a wrong job on the schedule.
    """
    from glass_guru.agents.llm.factory import build_llm
    from glass_guru.agents.router import NoteKind, route_note

    why = ""
    if kind is None:
        routed = route_note(build_llm(), request.text)
        kind = routed.value.kind if routed.value else NoteKind.BOOKING.value
        why = routed.value.why if routed.value else "could not tell, assumed a booking"

    if kind == NoteKind.DISRUPTION.value:
        return NoteView(kind=kind, why=why, disruption=run_triage(request))
    return NoteView(kind=NoteKind.BOOKING.value, why=why, booking=run_intake(request))


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


def _pricing(
    draft: Job | None, business: BusinessParams, driving: float, overtime: int = 0
) -> dict[str, Any]:
    """What to charge for this job in this slot, itemised.

    The driving figure differs per slot - that is the whole point of ranking them - so
    the margin does too, and a dispatcher can see that Tuesday is not just cheaper to
    serve but worth more.
    """
    if draft is None:
        return {}
    quote = quote_for(draft, business, driving_cost=driving, overtime_minutes=overtime)
    return {
        "quote_total": quote.total,
        "quote_lines": quote.explain(),
        "margin": round(quote.margin, 2),
        "margin_pct": round(quote.margin_pct, 1),
    }


_WEEKDAY = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")

#: How much warning a crew needs before a same-day job. Below this, today is not real.
_SAME_DAY_LEAD = timedelta(minutes=45)


def _booking_clock(
    svc: DispatchService, world: WorldState, duration_min: int
) -> tuple[date, datetime]:
    """Where the booking window opens, given what time it actually is.

    At 9:14 on a Wednesday evening the board offered "Wed, arrive 4:00 PM" - five
    hours gone - because the search never knew the time of day. Today stays on the
    table only while a job could still start, run, and finish inside somebody's
    reachable hours; after that the window rolls to the next working day and the
    caller still sees a full hand.
    """
    now = datetime.now(svc.tz)
    latest_end = 0
    overtime = int(svc.business.labor.overtime_max_minutes.value)
    for worker in world.workers.values():
        hours = worker.hours_for(now.weekday())
        if hours is None:
            continue
        end = hours.end.hour * 60 + hours.end.minute
        if worker.overtime_eligible:
            end += overtime
        latest_end = max(latest_end, end)
    start_minute = (now + _SAME_DAY_LEAD).hour * 60 + (now + _SAME_DAY_LEAD).minute
    today_works = latest_end > 0 and start_minute + duration_min <= latest_end
    start = next_working_day(world, now.date() if today_works else now.date() + timedelta(days=1))
    return start, now + _SAME_DAY_LEAD


def _capable_then(
    world: WorldState, draft: Job, weekday: int, start_hour: int, duration_min: int, overtime: int
) -> list[str]:
    """Who holds the certifications AND can be on site for the whole window."""
    names = []
    for worker in world.workers.values():
        if not draft.required_certifications <= worker.certifications:
            continue
        hours = worker.hours_for(weekday)
        if hours is None:
            continue
        end = hours.end.hour * 60 + hours.end.minute
        if worker.overtime_eligible:
            end += overtime
        if start_hour * 60 + duration_min <= end:
            names.append(worker.name)
    return sorted(names)


def _when_text(earliest: int | None, latest: int | None) -> str:
    """The caller's stated hours as a phrase for the intake form."""
    from datetime import time as _time

    if earliest is None and latest is None:
        return ""
    if earliest is not None and latest is not None:
        return clock_range(_time(earliest), _time(latest))
    if earliest is not None:
        return f"after {clock(_time(earliest))}"
    return f"before {clock(_time(latest or 0))}"


def _cover_note(capable: list[str], slot: SlotSuggestion) -> str:
    """Whether anyone else could take this slot. One name is a warning, not trivia.

    Worded around the actual bottleneck. A two-person crew where only Marcus holds the
    certification is not "the only fitter" - it is Marcus who cannot be replaced, and
    saying so tells the owner exactly who to train a backup for.
    """
    others = [n for n in capable if n not in slot.worker_names]
    if others:
        return f". Could also be covered by {', '.join(others)}"
    pinch = [n for n in slot.worker_names if n in capable]
    who = " and ".join(pinch) if pinch else "this crew"
    return f". No cover: {who} cannot be replaced for these hours - if they are out, this moves"


def _bottleneck_or(
    detail: str,
    world: WorldState,
    draft: Job,
    on_date: date,
    start_hour: int,
    overtime: int,
    tz: tzinfo,
) -> str:
    """Replace "no room at an acceptable cost" with the fact underneath it.

    Thursday was refused while three fitters and three vans sat free, and the message
    said cost. The truth was narrower: the one person certified for the work who can
    stay late enough was already booked then. A dispatcher can act on that - offer
    another day, or ask Dan - but not on "no room".
    """
    capable = _capable_then(
        world, draft, on_date.weekday(), start_hour, draft.estimated_duration_min, overtime
    )
    if not capable:
        needs = ", ".join(sorted(c.value.replace("_", " ") for c in draft.required_certifications))
        return f"nobody certified for {needs} can work these hours on a {on_date:%A}"
    busy = []
    for job in world.jobs.values():
        if job.commitment_state.value not in {"confirmed", "dispatched"} or not job.windows:
            continue
        window = job.windows[0]
        if window.start.astimezone(tz).date() == on_date:
            opens, closes = window.start.astimezone(tz), window.end.astimezone(tz)
            busy.append(f"{job.customer_name} {clock_range(opens, closes)}")
    if len(capable) == 1 and busy:
        return (
            f"{capable[0]} is the only fitter qualified who can work these hours, "
            f"and is already booked: {'; '.join(busy[:2])}"
        )
    return detail


def _crew_reason(draft: Job | None, slot: SlotSuggestion) -> str:
    """Why these people, in one line.

    Crew size and certifications come from the job catalogue, not from the model and
    not from the solver - the solver only decides *which* qualified people, and the
    answer to "why Marcus and Priya" is almost always "because two are needed and one
    of them is the only person certified for it".
    """
    if draft is None:
        return ""
    needs = ", ".join(sorted(c.value.replace("_", " ") for c in draft.required_certifications))
    people = "one fitter" if draft.crew_size == 1 else f"{draft.crew_size} fitters"
    return f"{people} needed" + (f", certified for {needs}" if needs else "")


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
            now=datetime.now(svc.tz),
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
        flexible: list[SlotView] = []
        unavailable: list[UnavailableDayView] = []
        if result.bookable and result.draft is not None:
            # A caller who stated nothing gets civil hours, not the crack of dawn:
            # Marcus starts at six, so an unconstrained solve offered every customer
            # 6:00 AM. But only when *nothing* was stated - "before we open at nine"
            # deliberately keeps early starts available, because arriving before the
            # doors open is the crew's business, not the caller's.
            earliest = result.earliest_hour
            if earliest is None and result.latest_hour is None:
                earliest = 8
            start, not_before = _booking_clock(svc, world, result.draft.estimated_duration_min)
            days = frozenset(result.preferred_weekdays) or None
            options = svc.booking_slots(
                result.draft,
                start,
                earliest_hour=earliest,
                latest_hour=result.latest_hour,
                not_before=not_before,
                allowed_weekdays=days,
            )

            # Who could actually take this work at these hours. One name is a single
            # point of failure the dispatcher should see before promising anything.
            overtime = int(svc.business.labor.overtime_max_minutes.value)
            capable = _capable_then(
                world,
                result.draft,
                start.weekday(),
                earliest or 8,
                result.draft.estimated_duration_min,
                overtime,
            )
            slots = [
                SlotView(
                    date=s.on_date.isoformat(),
                    day=f"{s.on_date:%a %d %b}",
                    window=clock_range(
                        s.quoted_window.start.astimezone(svc.tz),
                        s.quoted_window.end.astimezone(svc.tz),
                    ),
                    arrival=clock(s.arrival.astimezone(svc.tz)),
                    marginal_cost=round(s.marginal_cost, 2),
                    crew=" + ".join(s.worker_names),
                    crew_reason=_crew_reason(result.draft, s) + _cover_note(capable, s),
                    reason=s.reason,
                    needs_overtime=s.overtime_minutes > 0,
                    **_pricing(result.draft, svc.business, s.marginal_cost, s.overtime_minutes),
                )
                for s in options.slots
            ]

            # The stated hours cost real money when they run past a shift, and the
            # caller may not know that. When the stated hours start in the afternoon,
            # compute the in-hours alternatives too and hand them over separately -
            # the board shows them behind a tab, so the first thing on screen is what
            # was asked for and the cheaper options are one click deep, not mixed in.
            if slots and result.earliest_hour is not None and result.earliest_hour >= 12:
                relaxed = svc.booking_slots(
                    result.draft,
                    start,
                    earliest_hour=8,
                    not_before=not_before,
                    allowed_weekdays=days,
                )
                ceiling = min(s.quote_total for s in slots)
                for best in sorted(relaxed.slots, key=lambda r: r.marginal_cost)[:3]:
                    priced = _pricing(
                        result.draft, svc.business, best.marginal_cost, best.overtime_minutes
                    )
                    if priced["quote_total"] >= ceiling:
                        continue
                    flexible.append(
                        SlotView(
                            date=best.on_date.isoformat(),
                            day=f"{best.on_date:%a %d %b}",
                            window=clock_range(
                                best.quoted_window.start.astimezone(svc.tz),
                                best.quoted_window.end.astimezone(svc.tz),
                            ),
                            arrival=clock(best.arrival.astimezone(svc.tz)),
                            marginal_cost=round(best.marginal_cost, 2),
                            crew=" + ".join(best.worker_names),
                            crew_reason=_crew_reason(result.draft, best),
                            reason=best.reason,
                            needs_overtime=best.overtime_minutes > 0,
                            outside_preference=True,
                            **priced,
                        )
                    )

            # An empty list explains nothing. If the caller said "after four" and no
            # day can hold the work by then, that is the single most useful sentence
            # on the screen - it is what the dispatcher says back down the phone.
            unavailable = [
                UnavailableDayView(
                    day=f"{u.on_date:%a %d %b}",
                    reason=_bottleneck_or(
                        u.detail, world, result.draft, u.on_date, earliest or 8, overtime, svc.tz
                    ),
                )
                for u in options.unavailable
            ]

        return IntakeView(
            draft=draft,
            bookable=result.bookable,
            missing=list(result.missing_required),
            ask_next=list(result.ask_next),
            slots=slots,
            flexible_slots=flexible,
            unavailable=unavailable,
            repairs=result.extraction.repairs,
            note=result.geocode_note,
            when_text=_when_text(result.earliest_hour, result.latest_hour),
        )


@app.post("/api/book")
def book_slot(request: BookRequest) -> dict[str, str]:
    """Accept a quoted slot and put the job in the diary.

    The list of options was priced and then had no button, so a dispatcher could see
    that Tuesday was four times cheaper and had no way to act on it. This is that
    button.

    The draft comes back from the client rather than being held server-side between
    the two calls. Nothing an agent produced is stored until a person agrees to it,
    and a draft sitting in memory waiting to be confirmed is stored.

    The job is recorded as CONFIRMED with the window the customer was read out, which
    is what makes it expensive to move later - a promise, not a pencil mark.
    """
    from glass_guru.domain.enums import Certification, CommitmentState, ServiceType
    from glass_guru.domain.events import JobConfirmed, JobRequested
    from glass_guru.domain.models import GlassSpec, Location, Provenance, TimeWindow

    svc = service()
    draft = request.draft
    if draft.lat is None or draft.lon is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "NoLocation",
                "detail": "this draft has no address we could place on the map",
                "remedy": "add a street number and city, then read the slots again",
            },
        )

    with dispatch(new_dispatch_id("web")), span("api.book"):
        world = svc.world()
        # Wall time, not world.as_of. A replayed world's clock is its last event, so
        # stamping new events with it backdated every booking to the seed - and gave
        # book and cancel identical timestamps, where ordering falls to the random
        # event id and a cancellation can fold *before* the confirmation it undoes.
        now = datetime.now(svc.tz)
        job_id = f"j-{len(world.jobs) + 501}"
        arrival = datetime.fromisoformat(f"{request.date}T{request.arrival}").replace(tzinfo=svc.tz)
        # The customer is promised an *arrival* window; the stored window has to hold
        # the work as well, because the solver bounds completion by it and the checker
        # binds a confirmed job to it either way.
        #
        # Getting this wrong booked a three-hour storefront job into a two-hour window
        # and produced a plan with no routes in it at all: the job was impossible, so
        # nothing could be scheduled, and the board said "committed" and showed an
        # empty week.
        opens = arrival.replace(minute=arrival.minute // 15 * 15, second=0, microsecond=0)
        arrival_window = timedelta(minutes=svc.business.scheduling.quoted_window_minutes.value)
        quoted = arrival_window + timedelta(minutes=draft.duration_minutes)

        job = Job(
            id=job_id,
            customer_id=f"c-{job_id}",
            customer_name=draft.customer_name or "Unnamed caller",
            phone=draft.phone,
            location=Location(lat=draft.lat, lon=draft.lon, address=draft.address),
            service_type=ServiceType(draft.service_type),
            glass_spec=GlassSpec(),
            required_certifications=frozenset(Certification(c) for c in draft.certifications),
            crew_size=draft.crew_size,
            estimated_duration_min=draft.duration_minutes,
            duration_confidence_min=draft.duration_confidence,
            commitment_cost=draft.commitment_cost,
            site_notes=draft.site_notes,
            requested_at=now,
            windows=(TimeWindow(start=opens, end=opens + quoted),),
            commitment_state=CommitmentState.CONFIRMED,
            provenance=Provenance(
                source_channel="board", received_at=now, transcript=request.transcript
            ),
        )

        svc.apply_events(
            [
                JobRequested(
                    event_id=new_dispatch_id("job"),
                    occurred_at=now,
                    recorded_at=now,
                    dispatch_id="web",
                    job=job,
                ),
                JobConfirmed(
                    event_id=new_dispatch_id("confirm"),
                    occurred_at=now,
                    recorded_at=now,
                    dispatch_id="web",
                    job_id=job_id,
                    window=job.windows[0],
                    commitment_cost=draft.commitment_cost,
                ),
            ]
        )
        # Re-plan and commit, so the job is on the calendar the moment it is booked.
        # Recording the events alone left it in the world and invisible on the board,
        # which reads as the button having done nothing at all - the single worst
        # outcome for a button, because the next thing a dispatcher does is press it
        # again.
        placed = "unplaced"
        try:
            plan = svc.plan_week(start=_default_start(svc))
            head = svc.head()
            committed = svc.commit(plan.plan, expected_parent=head.id if head else None)
            placed = (
                "scheduled"
                if any(job_id in route.job_ids for route in committed.routes)
                else "booked but not yet scheduled"
            )
            broadcaster.publish("plan", {"plan_id": committed.id})
        except (ServiceError, PlanConflict):
            # The booking is recorded either way. A failed re-plan is a scheduling
            # problem to look at, not a reason to lose the customer's appointment.
            broadcaster.publish("world", {"booked": job_id})

        promised = job.windows[0].start.astimezone(svc.tz)
        finishes = arrival + timedelta(minutes=draft.duration_minutes)
        return {
            "job_id": job_id,
            "customer": job.customer_name,
            "when": f"{promised:%a %d %b} {clock_range(promised, finishes)}",
            "status": placed,
        }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, reason: str = Query(default="customer cancelled")) -> dict[str, str]:
    """Take a booking back out of the diary, fast.

    Customers change their minds on the same call that booked them, and until now the
    only route out was the CLI. The cancellation is an event like everything else - the
    log keeps that the booking happened and then did not, which is the difference
    between undoing and pretending.

    The re-plan runs immediately, because a cancelled job still sitting on the board is
    a slot a dispatcher will not offer to the next caller.
    """
    from glass_guru.domain.events import JobCancelled

    svc = service()
    with dispatch(new_dispatch_id("web")), span("api.cancel", job_id=job_id):
        world = svc.world()
        job = world.jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "NoSuchJob",
                    "detail": f"{job_id} is not in the diary",
                    "remedy": "it may already be cancelled; reload the board",
                },
            )
        now = datetime.now(svc.tz)
        svc.apply_events(
            [
                JobCancelled(
                    event_id=new_dispatch_id("cancel"),
                    occurred_at=now,
                    recorded_at=now,
                    dispatch_id="web",
                    job_id=job_id,
                    reason=reason,
                )
            ]
        )
        freed = "freed"
        try:
            plan = svc.plan_week(start=_default_start(svc))
            head = svc.head()
            svc.commit(plan.plan, expected_parent=head.id if head else None)
        except (ServiceError, PlanConflict):
            freed = "cancelled, but the re-plan needs attention"
        broadcaster.publish("plan", {"cancelled": job_id})
        return {"job_id": job_id, "customer": job.customer_name, "status": freed}


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
    if stream_mode() != "sse":
        # Refused rather than quietly served. An old tab that kept its connection would
        # go on billing for it, and a bill is a bad way to find out.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "StreamingDisabled",
                "detail": "this deployment is billed per second of open connection",
                "remedy": "the board polls instead; reload it to pick up the right mode",
            },
        )
    return StreamingResponse(
        broadcaster.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------------- helpers


def next_working_day(world: WorldState, on_or_after: date) -> date:
    """The first day anyone is rostered on, starting from the given date.

    Asked of the roster rather than assumed to be Monday to Friday, because the roster
    is where working days are actually written down and a business that starts opening
    Saturdays should not need a code change to be planned for.
    """
    rostered = {
        hours.weekday for worker in world.workers.values() for hours in worker.working_hours
    }
    if not rostered:
        return on_or_after
    for offset in range(14):
        candidate = on_or_after + timedelta(days=offset)
        if candidate.weekday() in rostered:
            return candidate
    return on_or_after


def next_working_day_from_today() -> date:
    """Fallback for a workspace that does not exist yet: assume a Monday-to-Friday
    week, which is what the roster would have said anyway."""
    today = datetime.now(UTC).date()
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() < 5:
            return candidate
    return today


def _default_start(svc: DispatchService) -> date:
    """Where the horizon begins.

    A committed plan keeps its own start, so re-reading the board does not silently
    slide the week out from under a dispatcher. Otherwise it is the next day anybody
    is working - which on a Saturday afternoon means Monday, and which is the whole
    point of a rolling horizon rather than a fixed one.

    This used to return a date written into the fixture, so on any day other than the
    week of 21 September 2026 the board showed a week that had already happened.
    """
    head = svc.head()
    if head is not None:
        return head.horizon_start
    return next_working_day(svc.world(), datetime.now(svc.tz).date())


def _clock(
    svc: DispatchService, value: Any, fallback: time | None, *, default_now: bool = False
) -> datetime | None:
    """A stated HH:MM, or - for the moment an event happened - when it was reported.

    Not 08:00. A van reported off the road at two in the afternoon was recorded as
    unavailable since breakfast, retroactively invalidating the work it had already
    done that morning.
    """
    if not value:
        if default_now:
            # Wall time. The fold clock is the LAST EVENT'S time, and stamping "now"
            # with it is the same bug that let a cancellation sort before the booking
            # it undid. "Dan is sick now" means now.
            return datetime.now(svc.tz)
        if fallback is None:
            return None
        return datetime.combine(_default_start(svc), fallback, tzinfo=svc.tz)
    # Two spellings. "17:30" is a time on the default day - what the CLI and the event
    # forms have always sent. A full ISO datetime is for a fact with a date in it:
    # "Marcus is out until Thursday" is not expressible as a clock reading, and forcing
    # it through one is how a one-day absence became a one-week absence on the board.
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        stamped = datetime.fromisoformat(text)
        return stamped if stamped.tzinfo else stamped.replace(tzinfo=svc.tz)
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
    # Loopback by default: running `glass-guru-api` on a laptop should not put an
    # unauthenticated dispatch board on the coffee-shop wifi. A container has to opt in
    # by setting the host, which its own Dockerfile does - and must, or nothing outside
    # the container can reach it, including a load balancer's health check.
    uvicorn.run(
        app,
        host=os.environ.get("GLASS_GURU_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("GLASS_GURU_API_PORT", "8000")),
    )


with suppress(ImportError):  # pragma: no cover - only for `python -m`
    pass
