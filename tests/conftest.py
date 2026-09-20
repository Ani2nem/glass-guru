"""Shared fixtures: the sample business, a deterministic travel oracle, and helpers
for assembling plans that tests then deliberately break."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.invariants import ValidationConfig
from glass_guru.domain.models import CrewRoute, JobId, PlanVersion, VanId, WorkerId
from glass_guru.domain.state import WorldState, fold
from glass_guru.domain.travel import TravelOracle
from glass_guru.fixtures.sample_business import BUSINESS_TZ, WEEK_START, _at, sample_world_at
from glass_guru.scheduler.routing import materialize_route
from glass_guru.scheduler.travel.factory import TravelMode, build_travel


@pytest.fixture
def travel() -> TravelOracle:
    """Real road distances from the committed snapshot: no network, no spend, and
    identical on every machine. A leg missing from the snapshot raises rather than
    silently degrading to a straight line."""
    return build_travel(BusinessParams.load(), TravelMode.FROZEN)


@pytest.fixture
def world() -> WorldState:
    events, now = sample_world_at()
    return fold(events, as_of=now)


@pytest.fixture
def config() -> ValidationConfig:
    return ValidationConfig(business_tz=BUSINESS_TZ)


class PlanBuilder:
    """Assembles routes into a plan, so tests read as "a valid plan, except ...""."""

    def __init__(self, world: WorldState, travel: TravelOracle) -> None:
        self.world = world
        self.travel = travel
        self.routes: list[CrewRoute] = []

    def route(
        self,
        crew_id: str,
        worker_ids: Sequence[WorkerId],
        van_id: VanId,
        job_sequence: Sequence[JobId],
        *,
        day: int = 0,
        start_hour: int = 8,
        start_minute: int = 0,
    ) -> CrewRoute:
        built = materialize_route(
            world=self.world,
            travel=self.travel,
            crew_id=crew_id,
            worker_ids=worker_ids,
            van_id=van_id,
            on_date=date.fromordinal(WEEK_START.toordinal() + day),
            job_sequence=job_sequence,
            shift_start=_at(day, start_hour, start_minute),
        )
        self.routes.append(built)
        return built

    def build(self, plan_id: str = "plan-1", parent_id: str | None = None) -> PlanVersion:
        return PlanVersion(
            id=plan_id,
            parent_id=parent_id,
            created_at=_at(0, 7),
            horizon_start=WEEK_START,
            horizon_end=date.fromordinal(WEEK_START.toordinal() + 4),
            routes=tuple(self.routes),
        )


@pytest.fixture
def plan_builder(world: WorldState, travel: TravelOracle) -> PlanBuilder:
    return PlanBuilder(world, travel)


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    """Timezone-aware moment relative to the sample week's Monday."""
    return _at(day, hour, minute)
