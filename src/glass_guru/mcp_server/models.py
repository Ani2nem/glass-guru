"""Result types for MCP tools.

Deliberately small and flat. These are read by a language model, not a human, and a
small one at that: every field it has to skip is a chance to misread the one that
matters. Nothing here returns a whole plan - a dispatcher's board is hundreds of
lines, and an agent asked to reason over it will reason over the wrong part of it.

Each tool answers one question with the fewest facts that settle it, and explanations
come pre-computed by the engine rather than left for the model to infer. An agent
should never have to work out *why* a job could not be scheduled; that is arithmetic,
and arithmetic belongs on the deterministic side of the line.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Closed schemas. Unknown fields are a bug, not a courtesy."""

    model_config = ConfigDict(extra="forbid")


class WorkerSummary(Strict):
    id: str
    name: str
    certifications: list[str]
    available_today: bool
    shift: str = ""


class VanSummary(Strict):
    id: str
    label: str
    available_today: bool
    stock: dict[str, int] = Field(default_factory=dict)


class JobSummary(Strict):
    id: str
    customer_name: str
    service_type: str
    duration_minutes: int
    crew_size: int
    required_certifications: list[str]
    commitment_state: str
    window: str = ""
    scheduled: str = ""


class WorldSummary(Strict):
    as_of: str
    workers: list[WorkerSummary]
    vans: list[VanSummary]
    jobs: list[JobSummary]
    committed_plan_id: str = ""
    notes: list[str] = Field(default_factory=list)


class UnservedSummary(Strict):
    job_id: str
    customer_name: str
    reason: str
    detail: str


class PlanSummary(Strict):
    """What a plan achieved, without the plan itself."""

    plan_id: str
    content_hash: str
    horizon_start: str
    horizon_end: str
    jobs_scheduled: int
    jobs_unserved: int
    crews_used: int
    total_cost: float
    feasible: bool
    violations: list[str] = Field(default_factory=list)
    unserved: list[UnservedSummary] = Field(default_factory=list)
    committed: bool = False


class SlotSummary(Strict):
    date: str
    window: str
    marginal_cost: float
    crew: str
    reason: str


class BookingSummary(Strict):
    slots: list[SlotSummary]
    unavailable: list[UnservedSummary] = Field(default_factory=list)
    days_considered: int = 0
    #: What choosing the cheapest option is worth on this call. The number that makes
    #: the whole feature pay for itself.
    savings_vs_worst: float = 0.0


class ChangeSummary(Strict):
    job_id: str
    customer_name: str
    kind: str
    description: str
    needs_customer_call: bool


class DiffSummary(Strict):
    before_plan_id: str
    after_plan_id: str
    summary: str
    blast_radius: str
    changes: list[ChangeSummary] = Field(default_factory=list)


class RepairCandidateSummary(Strict):
    strategy: str
    description: str
    jobs_served: int
    changes: int
    customer_calls: int
    blast_radius: str
    autonomy: str
    autonomy_reasons: list[str] = Field(default_factory=list)
    diff: list[ChangeSummary] = Field(default_factory=list)


class RepairSummary(Strict):
    baseline_plan_id: str
    candidates: list[RepairCandidateSummary]
    recommended: str = ""
    recommendation_reason: str = ""


class EventAck(Strict):
    recorded: bool
    event_type: str
    event_id: str
    dispatch_id: str
    message: str = ""


class ToolError(Strict):
    """A failure an agent can act on, rather than a stack trace it will paraphrase."""

    error: str
    detail: str
    remedy: str = ""
