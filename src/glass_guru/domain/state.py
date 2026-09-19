"""Deriving world state from the event log.

``WorldState`` is a *view*, never a stored fact: it is always ``fold(events, as_of)``.
Folding to an arbitrary ``as_of`` is what makes "what did we know at 10:52?" an
answerable question, which scenario replay and disruption evals both depend on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from glass_guru.domain.enums import CommitmentState
from glass_guru.domain.events import (
    CustomerRescheduled,
    Event,
    JobCancelled,
    JobCompleted,
    JobConfirmed,
    JobDeferred,
    JobDispatched,
    JobOverran,
    JobRequested,
    JobStarted,
    PlanCommitted,
    TrafficDelay,
    VanRegistered,
    VanRestored,
    VanUnavailable,
    WorkerRegistered,
    WorkerRestored,
    WorkerUnavailable,
)
from glass_guru.domain.models import (
    Frozen,
    Job,
    JobId,
    Van,
    VanId,
    Worker,
    WorkerId,
)


class Unavailability(Frozen):
    """A resource outage. ``until_time`` of ``None`` means open-ended."""

    from_time: datetime
    until_time: datetime | None = None
    reason: str = ""

    def covers(self, start: datetime, end: datetime) -> bool:
        """True if the outage overlaps the half-open interval ``[start, end)``."""
        if end <= self.from_time:
            return False
        return self.until_time is None or start < self.until_time


class TrafficOverride(Frozen):
    """A multiplier layered on the travel matrix for a corridor and period."""

    origin_geohash5: str | None
    dest_geohash5: str | None
    multiplier: float
    from_time: datetime
    until_time: datetime | None = None

    def applies_to(self, origin_gh5: str, dest_gh5: str, at: datetime) -> bool:
        if at < self.from_time or (self.until_time is not None and at >= self.until_time):
            return False
        if self.origin_geohash5 is not None and self.origin_geohash5 != origin_gh5:
            return False
        return not (self.dest_geohash5 is not None and self.dest_geohash5 != dest_gh5)


@dataclass
class WorldState:
    """Everything the solver and agents need to know, at one moment in time."""

    as_of: datetime
    workers: dict[WorkerId, Worker] = field(default_factory=dict)
    vans: dict[VanId, Van] = field(default_factory=dict)
    jobs: dict[JobId, Job] = field(default_factory=dict)
    worker_outages: dict[WorkerId, list[Unavailability]] = field(default_factory=dict)
    van_outages: dict[VanId, list[Unavailability]] = field(default_factory=dict)
    traffic_overrides: list[TrafficOverride] = field(default_factory=list)
    committed_plan_id: str | None = None
    applied_event_count: int = 0

    # ------------------------------------------------------------------ queries

    def active_jobs(self) -> list[Job]:
        """Jobs still needing service, oldest request first for stable ordering."""
        return sorted(
            (j for j in self.jobs.values() if j.is_active),
            key=lambda j: (j.requested_at, j.id),
        )

    def schedulable_jobs(self) -> list[Job]:
        """Active jobs that the solver may place - excludes in-flight work."""
        return [j for j in self.active_jobs() if j.commitment_state != CommitmentState.DISPATCHED]

    def is_worker_available(self, worker_id: WorkerId, start: datetime, end: datetime) -> bool:
        if worker_id not in self.workers:
            return False
        return not any(o.covers(start, end) for o in self.worker_outages.get(worker_id, []))

    def is_van_available(self, van_id: VanId, start: datetime, end: datetime) -> bool:
        if van_id not in self.vans:
            return False
        return not any(o.covers(start, end) for o in self.van_outages.get(van_id, []))

    def available_workers(self, start: datetime, end: datetime) -> list[Worker]:
        return [w for w in self.workers.values() if self.is_worker_available(w.id, start, end)]

    def available_vans(self, start: datetime, end: datetime) -> list[Van]:
        return [v for v in self.vans.values() if self.is_van_available(v.id, start, end)]

    def traffic_multiplier(self, origin_gh5: str, dest_gh5: str, at: datetime) -> float:
        """Product of every override in force. Overlapping delays compound."""
        multiplier = 1.0
        for override in self.traffic_overrides:
            if override.applies_to(origin_gh5, dest_gh5, at):
                multiplier *= override.multiplier
        return multiplier


def _close_latest_outage(outages: list[Unavailability], at: datetime) -> None:
    """Close the most recent open-ended outage. A restore with none open is a no-op."""
    for index in range(len(outages) - 1, -1, -1):
        if outages[index].until_time is None:
            outages[index] = outages[index].model_copy(update={"until_time": at})
            return


def _apply(state: WorldState, event: Event) -> None:
    """Apply one event. Structural matching keeps each branch narrowed to its own type."""
    match event:
        case WorkerRegistered():
            state.workers[event.worker.id] = event.worker

        case VanRegistered():
            state.vans[event.van.id] = event.van

        case JobRequested():
            job = event.job
            if job.commitment_state is CommitmentState.DRAFT:
                job = job.model_copy(update={"commitment_state": CommitmentState.PROVISIONAL})
            state.jobs[job.id] = job

        case JobConfirmed():
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={
                        "commitment_state": CommitmentState.CONFIRMED,
                        "commitment_cost": event.commitment_cost,
                        "windows": (event.window,),
                    }
                )

        case JobDispatched() | JobStarted():
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={"commitment_state": CommitmentState.DISPATCHED}
                )

        case JobCompleted():
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={
                        "commitment_state": CommitmentState.COMPLETED,
                        "estimated_duration_min": event.actual_duration_min,
                    }
                )

        case JobOverran():
            # The crew is still on site; the estimate was wrong and the rest of the
            # day is now at risk. Grow the duration so the re-solve sees reality.
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={
                        "estimated_duration_min": existing.estimated_duration_min
                        + event.extra_minutes
                    }
                )

        case JobCancelled():
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={"commitment_state": CommitmentState.CANCELLED}
                )

        case CustomerRescheduled():
            # A confirmed window the customer themselves moved is no longer a promise
            # we are keeping, so the commitment cost that protected it is released.
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={
                        "windows": event.new_windows,
                        "commitment_state": CommitmentState.PROVISIONAL,
                        "commitment_cost": 0.0,
                    }
                )

        case JobDeferred():
            if existing := state.jobs.get(event.job_id):
                state.jobs[existing.id] = existing.model_copy(
                    update={"deferral_count": existing.deferral_count + 1}
                )

        case WorkerUnavailable():
            state.worker_outages.setdefault(event.worker_id, []).append(
                Unavailability(
                    from_time=event.from_time,
                    until_time=event.until_time,
                    reason=event.reason,
                )
            )

        case WorkerRestored():
            _close_latest_outage(state.worker_outages.get(event.worker_id, []), event.occurred_at)

        case VanUnavailable():
            state.van_outages.setdefault(event.van_id, []).append(
                Unavailability(
                    from_time=event.from_time,
                    until_time=event.until_time,
                    reason=event.reason,
                )
            )

        case VanRestored():
            _close_latest_outage(state.van_outages.get(event.van_id, []), event.occurred_at)

        case TrafficDelay():
            state.traffic_overrides.append(
                TrafficOverride(
                    origin_geohash5=event.origin_geohash5,
                    dest_geohash5=event.dest_geohash5,
                    multiplier=event.multiplier,
                    from_time=event.from_time,
                    until_time=event.until_time,
                )
            )

        case PlanCommitted():
            state.committed_plan_id = event.plan_id

        case _:
            # JobSlotOffered, PlanProposed, ProposalApproved and ProposalRejected are
            # recorded for audit and eval labelling but do not mutate world state.
            pass


def fold(events: Iterable[Event], as_of: datetime | None = None) -> WorldState:
    """Replay the log into a world state.

    Events are applied in ``recorded_at`` order - the order we *learned* things -
    and any event recorded after ``as_of`` is excluded, so the result is exactly
    what was knowable at that moment.
    """
    ordered: Sequence[Event] = sorted(events, key=lambda e: (e.recorded_at, e.event_id))
    if as_of is not None:
        ordered = [e for e in ordered if e.recorded_at <= as_of]

    resolved_as_of = as_of or (ordered[-1].recorded_at if ordered else datetime.min)
    state = WorldState(as_of=resolved_as_of)
    for event in ordered:
        _apply(state, event)
    state.applied_event_count = len(ordered)
    return state
