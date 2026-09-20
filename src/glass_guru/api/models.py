"""Response shapes for the dispatch board.

Richer than the MCP models on purpose. Those are read by a small language model, where
every extra field is a chance to weigh the wrong one; these are read by a dispatcher
looking at a screen, where the extra context is the point. A person wants to see the
gap between two jobs and judge whether it looks wrong - a model would just be
distracted by it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Api(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StopView(Api):
    job_id: str
    customer_name: str
    service_type: str
    arrival: str
    departure: str
    #: Minutes from midnight, so the board can position bars without parsing dates.
    start_minute: int
    end_minute: int
    travel_minutes: int
    travel_miles: float
    crew_size: int
    commitment_state: str
    #: Slack before this stop. Surfaced because a large gap usually means the sequence
    #: is wrong, and that is a judgement a person makes by looking.
    gap_minutes: int = 0
    lat: float = 0.0
    lon: float = 0.0


class RouteView(Api):
    crew_id: str
    date: str
    worker_names: list[str]
    van_id: str
    stops: list[StopView]
    travel_minutes: int
    travel_miles: float
    idle_minutes: int
    utilization: float
    overtime_minutes: int


class UnservedView(Api):
    job_id: str
    customer_name: str
    reason: str
    detail: str
    is_failure: bool


class CostView(Api):
    travel_labor: float
    vehicle: float
    overtime: float
    lateness: float
    unserved: float
    total: float


class PlanView(Api):
    plan_id: str
    content_hash: str
    horizon_start: str
    horizon_end: str
    routes: list[RouteView]
    unserved: list[UnservedView]
    cost: CostView
    feasible: bool
    violations: list[str] = Field(default_factory=list)
    depot: list[float] = Field(default_factory=list)


class WorkerView(Api):
    id: str
    name: str
    certifications: list[str]
    shift: str
    available: bool
    overtime_eligible: bool


class VanView(Api):
    id: str
    label: str
    available: bool
    stock: dict[str, int]


class JobView(Api):
    id: str
    customer_name: str
    service_type: str
    duration_minutes: int
    crew_size: int
    certifications: list[str]
    commitment_state: str
    commitment_cost: float
    window: str
    lat: float
    lon: float


class WorldView(Api):
    as_of: str
    workers: list[WorkerView]
    vans: list[VanView]
    jobs: list[JobView]
    committed_plan_id: str = ""
    #: Shown on screen. Every cost here rests on numbers nobody has validated.
    calibration_warning: str = ""


class ChangeView(Api):
    job_id: str
    customer_name: str
    kind: str
    description: str
    needs_customer_call: bool


class CandidateView(Api):
    strategy: str
    description: str
    jobs_served: int
    changes: int
    customer_calls: int
    blast_radius: str
    autonomy: str
    autonomy_reasons: list[str]
    diff: list[ChangeView]
    recommended: bool = False


class RepairView(Api):
    baseline_plan_id: str
    candidates: list[CandidateView]
    recommended: str = ""
    rationale: str = ""
    #: Present when an agent, rather than the engine's default, made the choice.
    chosen_by: str = "engine"


class SlotView(Api):
    date: str
    window: str
    marginal_cost: float
    crew: str
    reason: str


class DraftView(Api):
    """A job taking shape during a call, including what is still missing."""

    customer_name: str = ""
    phone: str = ""
    address: str = ""
    service_type: str = ""
    duration_minutes: int = 0
    duration_confidence: int = 0
    crew_size: int = 0
    certifications: list[str] = Field(default_factory=list)
    commitment_cost: float = 0.0
    commitment_quotes: list[str] = Field(default_factory=list)
    lead_time_days: int = 0
    site_notes: str = ""
    lat: float | None = None
    lon: float | None = None


class IntakeView(Api):
    draft: DraftView
    bookable: bool
    missing: list[str]
    ask_next: list[str]
    slots: list[SlotView] = Field(default_factory=list)
    repairs: int = 0
    note: str = ""


class TriageView(Api):
    state: str
    summary: str
    events: list[dict[str, object]] = Field(default_factory=list)
    question: str = ""
    unknown_targets: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    repairs: int = 0


class MessageView(Api):
    job_id: str
    channel: str
    body: str
    grounded: bool
    issues: list[str] = Field(default_factory=list)


class ParamView(Api):
    path: str
    value: float
    source: str
    note: str


class ApiError(Api):
    error: str
    detail: str
    remedy: str = ""


# ------------------------------------------------------------------- requests


class EventRequest(Api):
    """One recorded fact. Validated here so a malformed body never reaches the log."""

    kind: str
    target: str | None = None
    at: str | None = Field(default=None, description="HH:MM")
    until: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    minutes: int | None = None
    multiplier: float | None = None
    commitment_cost: float = 0.0
    reason: str = ""


class TextRequest(Api):
    """Free text from a dispatcher, for triage or intake."""

    text: str


class AcceptRequest(Api):
    """Events a dispatcher reviewed and agreed to record."""

    events: list[dict[str, object]] = Field(default_factory=list)
