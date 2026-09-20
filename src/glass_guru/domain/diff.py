"""Comparing two plan versions.

A diff is the unit a dispatcher actually reviews. "Here is the new schedule" is
unreadable at 25 jobs a day; "three things changed, one of them touches a customer
who took the morning off work" is actionable in seconds.

It is also the input the comms agent works from. Drafting a customer message from a
structured diff rather than from a whole plan is what keeps the message grounded in
facts the engine computed, instead of facts a language model recalled.

The classification matters more than it looks. ``blast_radius`` is what the autonomy
policy keys on: a change that moves no confirmed window and contacts nobody can be
applied without asking, and one that moves a promised slot cannot. Getting that
distinction wrong in either direction is how a system like this loses trust - either
it nags about nothing, or it silently moves an appointment someone arranged their day
around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from glass_guru.domain.enums import CommitmentState
from glass_guru.domain.models import JobId, PlanVersion, TimeWindow, VanId, WorkerId
from glass_guru.domain.state import WorldState


class ChangeKind(StrEnum):
    ADDED = "added"
    DROPPED = "dropped"
    RESCHEDULED = "rescheduled"
    REASSIGNED = "reassigned"
    RETIMED = "retimed"


class BlastRadius(StrEnum):
    """How far a change reaches, worst wins across the whole diff."""

    #: Nothing a customer would notice: provisional work reshuffled.
    INTERNAL = "internal"
    #: A crew or van changed, but the customer's window did not.
    CREW_ONLY = "crew_only"
    #: A promised window moved, or a promised job was dropped. Someone must be told.
    CUSTOMER_VISIBLE = "customer_visible"


@dataclass(frozen=True, slots=True)
class Placement:
    on_date: date
    crew_id: str
    van_id: VanId
    worker_ids: tuple[WorkerId, ...]
    arrival: datetime
    departure: datetime
    #: Resolved at diff time. A dispatcher reads names, not ids.
    worker_names: tuple[str, ...] = ()

    @property
    def crew_label(self) -> str:
        return ", ".join(self.worker_names or self.worker_ids)


@dataclass(frozen=True, slots=True)
class JobChange:
    job_id: JobId
    kind: ChangeKind
    before: Placement | None
    after: Placement | None
    commitment_state: CommitmentState
    customer_name: str = ""
    #: What the customer was actually told, when anything was. A promise is a window,
    #: not a minute.
    promised_window: TimeWindow | None = None

    @property
    def customer_visible(self) -> bool:
        """Whether this change is one the customer would experience.

        Two rules, and the second is easy to get wrong in a way that quietly ruins the
        product. First, only promises count: moving provisional work is invisible by
        definition, because nobody was told about it, and that is exactly what makes
        provisional work the slack the optimiser is allowed to spend.

        Second, a promise is a *window*. A customer told "between nine and three" has
        not been let down when the crew arrives at 12:40 instead of 09:33. Treating
        every retime as a phone call would have the system ringing people to tell them
        nothing changed, and a dispatcher would stop reading the alerts within a week.
        """
        if self.commitment_state not in {
            CommitmentState.CONFIRMED,
            CommitmentState.DISPATCHED,
        }:
            return False
        if self.kind in {ChangeKind.DROPPED, ChangeKind.RESCHEDULED}:
            return True
        if self.kind is ChangeKind.RETIMED:
            if self.promised_window is None or self.after is None:
                return True
            return not self.promised_window.contains(self.after.arrival)
        return False

    def describe(self) -> str:
        name = self.customer_name or self.job_id
        if self.kind is ChangeKind.ADDED and self.after:
            return f"{name}: added {self.after.on_date:%a} {self.after.arrival:%H:%M}"
        if self.kind is ChangeKind.DROPPED and self.before:
            return f"{name}: dropped from {self.before.on_date:%a} {self.before.arrival:%H:%M}"
        if self.before and self.after:
            if self.kind is ChangeKind.RESCHEDULED:
                return (
                    f"{name}: {self.before.on_date:%a} {self.before.arrival:%H:%M}"
                    f" -> {self.after.on_date:%a} {self.after.arrival:%H:%M}"
                )
            if self.kind is ChangeKind.RETIMED:
                return (
                    f"{name}: {self.before.arrival:%H:%M} -> {self.after.arrival:%H:%M} (same crew)"
                )
            # Say which thing moved. A van swap with the same worker rendered as
            # "crew Dan -> Dan", which reads as a bug in the plan rather than a fact
            # about it.
            parts: list[str] = []
            if set(self.before.worker_ids) != set(self.after.worker_ids):
                parts.append(f"crew {self.before.crew_label} -> {self.after.crew_label}")
            if self.before.van_id != self.after.van_id:
                parts.append(f"van {self.before.van_id} -> {self.after.van_id}")
            return f"{name}: {', '.join(parts) or 'reassigned'}"
        return f"{name}: {self.kind.value}"


@dataclass(frozen=True, slots=True)
class PlanDiff:
    changes: tuple[JobChange, ...]
    cost_delta: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.changes)

    @property
    def blast_radius(self) -> BlastRadius:
        if any(c.customer_visible for c in self.changes):
            return BlastRadius.CUSTOMER_VISIBLE
        if any(c.kind is ChangeKind.REASSIGNED for c in self.changes):
            return BlastRadius.CREW_ONLY
        return BlastRadius.INTERNAL

    @property
    def customer_visible_changes(self) -> tuple[JobChange, ...]:
        return tuple(c for c in self.changes if c.customer_visible)

    def by_kind(self, kind: ChangeKind) -> tuple[JobChange, ...]:
        return tuple(c for c in self.changes if c.kind is kind)

    def summary(self) -> str:
        if not self.changes:
            return "no changes"
        counts: dict[ChangeKind, int] = {}
        for change in self.changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        parts = [f"{count} {kind.value}" for kind, count in sorted(counts.items())]
        visible = len(self.customer_visible_changes)
        tail = f", {visible} customer-visible" if visible else ""
        return f"{len(self.changes)} change(s): {', '.join(parts)}{tail}"


def _placements(plan: PlanVersion, world: WorldState | None) -> dict[JobId, Placement]:
    def names(worker_ids: tuple[WorkerId, ...]) -> tuple[str, ...]:
        if world is None:
            return ()
        return tuple(world.workers[w].name if w in world.workers else w for w in worker_ids)

    return {
        stop.job_id: Placement(
            on_date=route.date,
            crew_id=route.crew_id,
            van_id=route.van_id,
            worker_ids=route.worker_ids,
            arrival=stop.arrival,
            departure=stop.departure,
            worker_names=names(route.worker_ids),
        )
        for route in plan.routes
        for stop in route.stops
    }


def _classify(before: Placement, after: Placement) -> ChangeKind | None:
    if before.on_date != after.on_date:
        return ChangeKind.RESCHEDULED
    if before.arrival != after.arrival:
        # Same day, same crew, different time is a retime; different crew as well is
        # still primarily a retime from the customer's point of view.
        return ChangeKind.RETIMED
    if set(before.worker_ids) != set(after.worker_ids) or before.van_id != after.van_id:
        return ChangeKind.REASSIGNED
    return None


def diff_plans(
    before: PlanVersion,
    after: PlanVersion,
    world: WorldState | None = None,
    cost_delta: float = 0.0,
) -> PlanDiff:
    """What changed between two plans, classified by who would notice."""
    old, new = _placements(before, world), _placements(after, world)
    changes: list[JobChange] = []

    def _promise(job_id: JobId) -> tuple[CommitmentState, str, TimeWindow | None]:
        job = world.jobs.get(job_id) if world else None
        if job is None:
            return CommitmentState.PROVISIONAL, "", None
        # On JobConfirmed the fold replaces the job's windows with the single window
        # the customer was quoted, so that is the promise to measure against.
        promised = (
            job.windows[0]
            if job.commitment_state is CommitmentState.CONFIRMED and job.windows
            else None
        )
        return job.commitment_state, job.customer_name, promised

    for job_id in sorted(set(old) | set(new)):
        state, name, promised = _promise(job_id)
        previous, current = old.get(job_id), new.get(job_id)

        if previous is None and current is not None:
            kind = ChangeKind.ADDED
        elif previous is not None and current is None:
            kind = ChangeKind.DROPPED
        else:
            assert previous is not None and current is not None
            classified = _classify(previous, current)
            if classified is None:
                continue
            kind = classified

        changes.append(
            JobChange(
                job_id=job_id,
                kind=kind,
                before=previous,
                after=current,
                commitment_state=state,
                customer_name=name,
                promised_window=promised,
            )
        )

    return PlanDiff(changes=tuple(changes), cost_delta=cost_delta)
