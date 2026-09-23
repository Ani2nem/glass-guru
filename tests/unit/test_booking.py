"""Marginal-cost booking suggestions.

Drafts reuse existing fixture locations so every leg is in the frozen snapshot -
these tests run on real road distances, offline.

The property that matters most is the displacement one: a quote must never look
cheap because it quietly moved a customer who had already been promised a slot.
"""

from __future__ import annotations

from datetime import date

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import Certification, ServiceType
from glass_guru.domain.models import Job, TimeWindow
from glass_guru.domain.state import WorldState
from glass_guru.fixtures.sample_business import WEEK_START, _at
from glass_guru.scheduler.booking import BookingOptions, suggest_booking_slots
from glass_guru.scheduler.day_planner import SolveParams


@pytest.fixture
def business() -> BusinessParams:
    return BusinessParams.load()


@pytest.fixture
def params(business: BusinessParams) -> SolveParams:
    from zoneinfo import ZoneInfo

    return SolveParams.from_business(business, ZoneInfo(business.meta.timezone))


HORIZON = [date.fromordinal(WEEK_START.toordinal() + i) for i in range(5)]


def draft_at(
    world: WorldState,
    like_job: str,
    *,
    certs: set[Certification] | None = None,
    duration: int = 90,
    crew: int = 1,
) -> Job:
    """A prospective job at an existing site, so the frozen snapshot covers it."""
    return Job(
        id="draft",
        customer_id="c-draft",
        customer_name="New caller",
        location=world.jobs[like_job].location,
        service_type=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
        required_certifications=frozenset(
            certs if certs is not None else {Certification.RESIDENTIAL_GLAZING}
        ),
        crew_size=crew,
        estimated_duration_min=duration,
        revenue=700.0,
        windows=tuple(TimeWindow(start=_at(d, 8), end=_at(d, 17)) for d in range(5)),
        requested_at=_at(0, 7),
    )


def quote(world, travel, params, business, draft: Job) -> BookingOptions:
    return suggest_booking_slots(
        world=world,
        travel=travel,
        draft=draft,
        horizon=HORIZON,
        params=params,
        business=business,
    )


# --------------------------------------------------------------------- ranking


def test_slots_are_ranked_cheapest_first(world, travel, params, business):
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    costs = [s.marginal_cost for s in options.slots]
    assert costs == sorted(costs)
    assert options.best is options.slots[0]


def test_clustering_is_cheaper_than_an_isolated_trip(world, travel, params, business):
    """The whole point. A job beside existing stops costs a detour; a job on its own
    costs a round trip, and the difference is what a dispatcher should be steering by."""
    near = quote(world, travel, params, business, draft_at(world, "j-402"))
    assert near.best is not None
    cheapest, dearest = near.slots[0], near.slots[-1]
    assert cheapest.marginal_cost < dearest.marginal_cost
    assert near.savings_vs_worst > 0


def test_the_cheapest_slot_shares_a_day_with_nearby_work(world, travel, params, business):
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    assert options.best is not None
    assert options.best.added_travel_minutes < options.slots[-1].added_travel_minutes


def test_a_dedicated_trip_is_explained_as_one(world, travel, params, business):
    """A day with nothing else on it means the whole round trip is this job's.

    The assertion used to be that a dedicated trip is the dearest option, which was
    true of the old geography and is not a property of anything. Moving the business
    produced a day costing twice the dedicated trip on six minutes of extra driving:
    it was the fullest day in the week, and the insertion tipped a crew into overtime.
    Cheaper to drive half an hour on an empty Friday than to buy an hour of overtime
    on a full Monday, which is the sort of thing the dollar objective exists to notice.

    What a dedicated trip does guarantee is that none of its travel is shared, so its
    *added travel* is the largest. That is the property worth asserting.
    """
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    lonely = [s for s in options.slots if "dedicated trip" in s.reason]
    assert lonely, "an otherwise-empty day should be described as a dedicated trip"
    assert lonely[-1].added_travel_minutes == max(s.added_travel_minutes for s in options.slots)


# ------------------------------------------------------------------ safety


def test_quoting_never_displaces_already_placed_work(world, travel, params, business):
    """A quote that looked cheap because it bumped a promised customer would be
    worse than useless. Existing work is locked while the trial insertion runs."""
    from glass_guru.scheduler.day_planner import plan_day

    draft = draft_at(world, "j-402")
    baseline = plan_day(
        world=world,
        travel=travel,
        on_date=WEEK_START,
        candidate_job_ids=[j.id for j in world.active_jobs()],
        params=params,
    )
    placed_before = {j for route in baseline.routes for j in route.job_ids}

    options = quote(world, travel, params, business, draft)
    monday = [s for s in options.slots if s.on_date == WEEK_START]
    assert monday, "Monday should be bookable"

    after = plan_day(
        world=world,
        travel=travel,
        on_date=WEEK_START,
        candidate_job_ids=[j.id for j in world.active_jobs()],
        params=params,
    )
    assert {j for route in after.routes for j in route.job_ids} == placed_before


def test_marginal_cost_is_never_negative(world, travel, params, business):
    """Adding work cannot make a day cheaper to run."""
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    assert all(s.marginal_cost >= 0 for s in options.slots)


def test_quoted_window_is_the_configured_width_and_contains_the_arrival(
    world, travel, params, business
):
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    expected = business.scheduling.quoted_window_minutes.value
    for slot in options.slots:
        width = (slot.quoted_window.end - slot.quoted_window.start).total_seconds() / 60
        assert width == expected
        assert slot.quoted_window.contains(slot.arrival)


# ----------------------------------------------------------------- constraints


def test_a_scarce_certification_gates_every_offered_crew(world, travel, params, business):
    """Only Marcus and Priya hold the commercial certification. Certifications pool
    across a crew, so the second seat can be anyone - but one of those two must be on
    every crew offered, which is what makes storefront work the bottleneck."""
    draft = draft_at(
        world, "j-401", certs={Certification.COMMERCIAL_STOREFRONT}, duration=120, crew=2
    )
    options = quote(world, travel, params, business, draft)
    assert options.slots
    for slot in options.slots:
        assert len(slot.worker_names) == 2
        assert {"Marcus", "Priya"} & set(slot.worker_names), (
            f"{slot.worker_names} has nobody commercially certified"
        )


def test_unbookable_days_are_explained(world, travel, params, business):
    """A job needing a certification nobody has should report why, not go silent."""
    from glass_guru.domain.state import Unavailability

    for worker_id in ("w-marcus", "w-priya"):
        world.worker_outages[worker_id] = [
            Unavailability(from_time=_at(0, 0), until_time=_at(5, 0), reason="out")
        ]
    draft = draft_at(
        world, "j-401", certs={Certification.COMMERCIAL_STOREFRONT}, duration=120, crew=2
    )
    options = quote(world, travel, params, business, draft)
    assert options.slots == ()
    assert options.unavailable
    assert all(u.detail for u in options.unavailable)


def test_every_day_in_the_horizon_is_considered(world, travel, params, business):
    options = quote(world, travel, params, business, draft_at(world, "j-402"))
    assert options.evaluated_days == len(HORIZON)
    assert len(options.slots) + len(options.unavailable) == len(HORIZON)
