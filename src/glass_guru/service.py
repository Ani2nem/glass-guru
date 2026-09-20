"""The operations the business actually performs, in one place.

Everything above this line - CLI, MCP tools, and later the HTTP API and the agents -
is a different way of asking for the same handful of things: what does the world look
like, plan the week, price a job into it, repair after a disruption, commit the
result. Writing those once means an agent and a dispatcher cannot end up with
subtly different behaviour, which is a failure mode worth designing out rather than
testing for.

It is also where tracing belongs. The solver and the domain stay free of any
observability imports; this layer opens the spans and records what came back, so a
whole episode joins up under one dispatch id without the pure code knowing tracing
exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from glass_guru.config import BusinessParams
from glass_guru.domain.autonomy import AutonomyDecision, AutonomyPolicy, decide
from glass_guru.domain.diff import PlanDiff, diff_plans
from glass_guru.domain.events import Event
from glass_guru.domain.invariants import ValidationConfig, Violation, validate_plan
from glass_guru.domain.models import CostBreakdown, CrewRoute, Job, PlanVersion
from glass_guru.domain.state import WorldState, fold
from glass_guru.obs.correlation import current_dispatch_id, dispatch
from glass_guru.obs.tracing import record, span
from glass_guru.persistence.log import PlanConflict, Workspace
from glass_guru.scheduler.booking import BookingOptions, suggest_booking_slots
from glass_guru.scheduler.costing import RouteCost, cost_plan
from glass_guru.scheduler.day_planner import SolveParams
from glass_guru.scheduler.horizon import HorizonParams, HorizonResult, plan_horizon
from glass_guru.scheduler.repair import (
    RepairOptions,
    carve_out_locked,
    locked_jobs,
    repair_plan,
)
from glass_guru.scheduler.travel.base import OverrideAdjustedProvider, TravelProvider
from glass_guru.scheduler.travel.factory import TravelMode, build_travel


class ServiceError(RuntimeError):
    """Something the caller can act on: no workspace, no committed plan, a conflict."""


@dataclass(frozen=True, slots=True)
class PlanResult:
    """A plan together with everything needed to judge it."""

    plan: PlanVersion
    horizon: HorizonResult
    violations: tuple[Violation, ...]
    cost: CostBreakdown
    route_costs: tuple[RouteCost, ...]

    @property
    def feasible(self) -> bool:
        return not self.violations


class DispatchService:
    """One business, one workspace."""

    def __init__(
        self,
        workspace: Workspace,
        business: BusinessParams | None = None,
        travel_mode: TravelMode | str = TravelMode.AUTO,
    ) -> None:
        self.workspace = workspace
        self.business = business or BusinessParams.load()
        self.travel_mode = TravelMode(travel_mode)
        self.tz = ZoneInfo(self.business.meta.timezone)

    # ------------------------------------------------------------------ context

    def world(self, as_of: datetime | None = None) -> WorldState:
        if not self.workspace.exists:
            raise ServiceError(
                f"no workspace at {self.workspace.root}; create one with `glass-guru init`"
            )
        return fold(self.workspace.events.read(), as_of=as_of)

    def travel(self, world: WorldState) -> TravelProvider:
        """Travel with the world's recorded traffic delays layered on.

        Providers stay pure; a TrafficDelay event reaches routing without any provider
        knowing the event log exists.
        """
        base = build_travel(self.business, self.travel_mode)
        if not world.traffic_overrides:
            return base
        return OverrideAdjustedProvider(base, world.traffic_multiplier)

    def solve_params(self, *, allow_overtime: bool = True) -> SolveParams:
        return SolveParams.from_business(self.business, self.tz, allow_overtime=allow_overtime)

    def horizon_params(self) -> HorizonParams:
        return HorizonParams.from_business(self.business)

    def policy(self) -> AutonomyPolicy:
        return AutonomyPolicy.from_business(self.business)

    def head(self) -> PlanVersion | None:
        return self.workspace.plans.head()

    # ------------------------------------------------------------------- events

    def apply_events(self, events: Sequence[Event]) -> int:
        """Append to the log. The only way world state ever changes."""
        with span("events.apply", count=len(events), kinds=[e.type for e in events]):
            self.workspace.events.append(events)
            return len(events)

    # ------------------------------------------------------------------ planning

    def plan_week(
        self,
        start: date,
        *,
        allow_overtime: bool = True,
        world: WorldState | None = None,
    ) -> PlanResult:
        """Plan the rolling horizon and check it. Does not commit.

        Work already dispatched is carried forward rather than re-planned. Planning a
        day from scratch at eleven in the morning would otherwise silently drop the
        crew that left at six - those jobs are no longer "schedulable", so they simply
        vanish from the result. Repair already knew this; a plain re-plan did not, and
        a dispatcher pressing the button mid-morning would have erased work in
        progress without being told.
        """
        with span("plan.horizon", start=start.isoformat()) as active:
            state = world if world is not None else self.world()
            params = self.solve_params(allow_overtime=allow_overtime)
            horizon_params = self.horizon_params()

            head = self.head()
            locked = locked_jobs(state)
            carried: tuple[CrewRoute, ...] = ()
            planning_state = state
            if head is not None and locked:
                planning_state, carried = carve_out_locked(state, head, locked)

            travel = self.travel(planning_state)
            result = plan_horizon(
                world=planning_state,
                travel=travel,
                start=start,
                params=params,
                horizon_params=horizon_params,
            )
            plan = PlanVersion(
                id=self._next_plan_id(),
                parent_id=head.id if head else None,
                created_at=state.as_of,
                horizon_start=start,
                horizon_end=date.fromordinal(start.toordinal() + horizon_params.days - 1),
                routes=(*carried, *result.routes),
                unserved=result.unserved,
                label="committed",
            )
            violations = validate_plan(
                plan,
                state,
                travel,
                ValidationConfig(business_tz=self.tz, allow_overtime=allow_overtime),
            )
            cost, route_costs = cost_plan(plan, state, self.business, self.tz, result.unserved)

            record(
                metrics=result.metrics,
                violations=len(violations),
                content_hash=plan.content_hash,
                cost_total=round(cost.total, 2),
            )
            active.set_attribute("feasible", not violations)
            return PlanResult(plan, result, violations, cost, tuple(route_costs))

    def commit(self, plan: PlanVersion, expected_parent: str | None) -> PlanVersion:
        """Store a plan as the new head.

        Refuses an infeasible plan and refuses a stale one. Both are guards rather
        than tests: nothing that fails its own invariants should ever be stored, and a
        stale write silently discards whatever another dispatcher just committed.
        """
        with span("plan.commit", plan_id=plan.id, parent=expected_parent or ""):
            try:
                committed = self.workspace.plans.commit(plan, expected_parent)
            except PlanConflict as exc:
                raise ServiceError(str(exc)) from exc
            record(content_hash=committed.content_hash)
            return committed

    def repair(self, world: WorldState | None = None) -> tuple[RepairOptions, PlanVersion]:
        """Repair the committed plan. Returns the candidates and the baseline."""
        baseline = self.head()
        if baseline is None:
            raise ServiceError("nothing committed yet; run `glass-guru commit` first")

        with span("plan.repair", baseline=baseline.id) as active:
            state = world if world is not None else self.world()
            options = repair_plan(
                world=state,
                travel=self.travel(state),
                baseline=baseline,
                start=baseline.horizon_start,
                params=self.solve_params(),
                horizon_params=self.horizon_params(),
            )
            best = options.best_by_fewest_calls
            record(
                candidates=len(options.candidates),
                recommended=best.strategy.name if best else "",
                customer_calls=best.customer_calls if best else -1,
            )
            active.set_attribute("blast_radius", best.diff.blast_radius.value if best else "")
            return options, baseline

    def autonomy(self, diff: PlanDiff, *, added_overtime_minutes: int = 0) -> AutonomyDecision:
        decision = decide(diff, self.policy(), added_overtime_minutes=added_overtime_minutes)
        record(autonomy=decision.decision.value)
        return decision

    # ------------------------------------------------------------------ booking

    def booking_slots(self, draft: Job, start: date) -> BookingOptions:
        with span("booking.slots", service=draft.service_type.value) as active:
            state = self.world()
            horizon_params = self.horizon_params()
            horizon = [
                date.fromordinal(start.toordinal() + offset)
                for offset in range(horizon_params.days)
            ]
            options = suggest_booking_slots(
                world=state,
                travel=self.travel(state),
                draft=draft,
                horizon=horizon,
                params=self.solve_params(),
                business=self.business,
            )
            active.set_attribute("slots", len(options.slots))
            record(
                best_cost=round(options.best.marginal_cost, 2) if options.best else -1,
                spread=round(options.savings_vs_worst, 2),
            )
            return options

    # -------------------------------------------------------------------- diffs

    def diff(self, before_id: str, after_id: str) -> PlanDiff:
        before = self.workspace.plans.get(before_id)
        after = self.workspace.plans.get(after_id)
        if before is None or after is None:
            missing = before_id if before is None else after_id
            raise ServiceError(f"no such plan version: {missing}")
        return diff_plans(before, after, self.world())

    # ----------------------------------------------------------------- internals

    def _next_plan_id(self) -> str:
        return f"v{len(self.workspace.plans.history()) + 1:03d}"


def traced_dispatch(dispatch_id: str | None = None) -> object:
    """Convenience re-export so callers need only import from this module."""
    return dispatch(dispatch_id)


__all__ = [
    "DispatchService",
    "PlanResult",
    "ServiceError",
    "current_dispatch_id",
    "dispatch",
]
