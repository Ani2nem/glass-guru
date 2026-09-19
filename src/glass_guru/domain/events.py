"""The append-only event log.

World state is ``fold(events)``. Nothing else is authoritative. This is what lets
any scenario - a van breaking down at 10:40 on a Tuesday - replay byte-identically
in a test, which is the precondition for the entire eval layer.

Every event carries a ``dispatch_id``: the correlation ID minted the moment raw
text enters the system, propagated through agents, MCP calls, and the solver, so a
whole disruption is one trace end to end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from glass_guru.domain.models import (
    Frozen,
    Job,
    JobId,
    TimeWindow,
    Van,
    VanId,
    Worker,
    WorkerId,
)


class EventBase(Frozen):
    """Common envelope.

    ``occurred_at`` is when the thing happened in the world; ``recorded_at`` is when
    we learned about it. They differ constantly in field service - a van breaks at
    10:40 and the dispatcher hears about it at 10:52 - and conflating them corrupts
    any replay that depends on what was knowable at a given moment.
    """

    event_id: str
    occurred_at: datetime
    recorded_at: datetime
    dispatch_id: str
    actor: str = "system"


# --------------------------------------------------------------------------- roster


class WorkerRegistered(EventBase):
    type: Literal["worker_registered"] = "worker_registered"
    worker: Worker


class VanRegistered(EventBase):
    type: Literal["van_registered"] = "van_registered"
    van: Van


# ------------------------------------------------------------------------- job life


class JobRequested(EventBase):
    type: Literal["job_requested"] = "job_requested"
    job: Job


class JobSlotOffered(EventBase):
    """Slots quoted to a customer on a call. Recorded even if they decline, because
    a declined cheap slot followed by an accepted expensive one is a real signal."""

    type: Literal["job_slot_offered"] = "job_slot_offered"
    job_id: JobId
    offered: tuple[TimeWindow, ...]
    marginal_costs: tuple[float, ...] = ()


class JobConfirmed(EventBase):
    """The customer was given a window. From here the window costs money to move."""

    type: Literal["job_confirmed"] = "job_confirmed"
    job_id: JobId
    window: TimeWindow
    commitment_cost: float = 0.0


class JobDispatched(EventBase):
    type: Literal["job_dispatched"] = "job_dispatched"
    job_id: JobId


class JobStarted(EventBase):
    type: Literal["job_started"] = "job_started"
    job_id: JobId


class JobCompleted(EventBase):
    type: Literal["job_completed"] = "job_completed"
    job_id: JobId
    actual_duration_min: int = Field(gt=0)


class JobOverran(EventBase):
    """In-progress job is taking longer than estimated. Distinct from completion -
    it arrives while the crew is still on site and the rest of the day is at risk."""

    type: Literal["job_overran"] = "job_overran"
    job_id: JobId
    extra_minutes: int = Field(gt=0)


class JobCancelled(EventBase):
    type: Literal["job_cancelled"] = "job_cancelled"
    job_id: JobId
    reason: str = ""


class CustomerRescheduled(EventBase):
    type: Literal["customer_rescheduled"] = "customer_rescheduled"
    job_id: JobId
    new_windows: tuple[TimeWindow, ...]


class JobDeferred(EventBase):
    """Job pushed out of the horizon. Increments ``deferral_count``, which raises its
    unserved penalty so the optimizer cannot quietly defer it forever."""

    type: Literal["job_deferred"] = "job_deferred"
    job_id: JobId


# ----------------------------------------------------------------------- resources


class WorkerUnavailable(EventBase):
    type: Literal["worker_unavailable"] = "worker_unavailable"
    worker_id: WorkerId
    from_time: datetime
    until_time: datetime | None = None
    reason: str = ""


class WorkerRestored(EventBase):
    type: Literal["worker_restored"] = "worker_restored"
    worker_id: WorkerId


class VanUnavailable(EventBase):
    type: Literal["van_unavailable"] = "van_unavailable"
    van_id: VanId
    from_time: datetime
    until_time: datetime | None = None
    reason: str = ""


class VanRestored(EventBase):
    type: Literal["van_restored"] = "van_restored"
    van_id: VanId


class TrafficDelay(EventBase):
    """A multiplier layered on top of the travel matrix.

    ``None`` on either endpoint means "any", so a region-wide slowdown is one event
    rather than a fan-out over every corridor pair.
    """

    type: Literal["traffic_delay"] = "traffic_delay"
    origin_geohash5: str | None = None
    dest_geohash5: str | None = None
    multiplier: float = Field(gt=0)
    from_time: datetime
    until_time: datetime | None = None
    note: str = ""


# ---------------------------------------------------------------------------- plan


class PlanCommitted(EventBase):
    type: Literal["plan_committed"] = "plan_committed"
    plan_id: str
    parent_id: str | None = None
    content_hash: str = ""


class PlanProposed(EventBase):
    type: Literal["plan_proposed"] = "plan_proposed"
    proposal_id: str
    candidate_plan_ids: tuple[str, ...]
    recommended_plan_id: str
    rationale: str = ""


class ProposalApproved(EventBase):
    """Dispatcher accepted a proposal. Doubles as a ground-truth eval label."""

    type: Literal["proposal_approved"] = "proposal_approved"
    proposal_id: str
    chosen_plan_id: str
    note: str = ""


class ProposalRejected(EventBase):
    type: Literal["proposal_rejected"] = "proposal_rejected"
    proposal_id: str
    note: str = ""


Event = Annotated[
    WorkerRegistered
    | VanRegistered
    | JobRequested
    | JobSlotOffered
    | JobConfirmed
    | JobDispatched
    | JobStarted
    | JobCompleted
    | JobOverran
    | JobCancelled
    | CustomerRescheduled
    | JobDeferred
    | WorkerUnavailable
    | WorkerRestored
    | VanUnavailable
    | VanRestored
    | TrafficDelay
    | PlanCommitted
    | PlanProposed
    | ProposalApproved
    | ProposalRejected,
    Field(discriminator="type"),
]
