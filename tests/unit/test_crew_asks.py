"""The ask-the-crew loop: hours nobody can work become a stored question, a yes
becomes a one-day shift extension, and the extension makes the booking real."""

from __future__ import annotations

from datetime import timedelta

from krama.domain.events import (
    CrewAskClosed,
    CrewAskOpened,
    ShiftExtended,
    VanRemoved,
    WorkerRemoved,
)
from krama.domain.state import fold
from krama.fixtures.sample_business import BUSINESS_TZ, WEEK_START, _at, seed_events


def _extend(worker: str, hour: int, event_id: str = "e-ext") -> ShiftExtended:
    return ShiftExtended(
        event_id=event_id,
        occurred_at=_at(0, 6),
        recorded_at=_at(0, 6),
        dispatch_id="t",
        worker_id=worker,
        on_date=WEEK_START,
        until_time=_at(0, hour),
    )


def test_two_agreements_keep_the_later_hour():
    world = fold([*seed_events(), _extend("w-dan", 19), _extend("w-dan", 21, "e-ext2")])
    assert world.extension_for("w-dan", WEEK_START) == _at(0, 21)
    world = fold([*seed_events(), _extend("w-dan", 21), _extend("w-dan", 19, "e-ext2")])
    assert world.extension_for("w-dan", WEEK_START) == _at(0, 21), (
        '"I can stay to seven" does not cancel "I can stay to nine"'
    )


def test_an_extension_is_for_one_date_only():
    world = fold([*seed_events(), _extend("w-dan", 20)])
    assert world.extension_for("w-dan", WEEK_START + timedelta(days=1)) is None


def test_an_ask_opens_red_and_closes_gone():
    opened = CrewAskOpened(
        event_id="e-ask",
        occurred_at=_at(0, 9),
        recorded_at=_at(0, 9),
        dispatch_id="t",
        ask_id="ask-1",
        customer_name="Jimmy",
        phone="8175768492",
        transcript="walmart glass broke, come at 5pm",
        on_date=WEEK_START,
        until_time=_at(0, 20),
        candidate_ids=("w-marcus", "w-priya"),
    )
    world = fold([*seed_events(), opened])
    assert world.crew_asks["ask-1"].customer_name == "Jimmy"

    closed = CrewAskClosed(
        event_id="e-done",
        occurred_at=_at(0, 10),
        recorded_at=_at(0, 10),
        dispatch_id="t",
        ask_id="ask-1",
        outcome="booked",
    )
    assert "ask-1" not in fold([*seed_events(), opened, closed]).crew_asks


def test_removing_a_worker_takes_them_off_the_roster():
    gone = WorkerRemoved(
        event_id="e-rm",
        # After the last seed event, or the fold applies the removal before the
        # registration it removes.
        occurred_at=_at(0, 8),
        recorded_at=_at(0, 8),
        dispatch_id="t",
        worker_id="w-sofia",
    )
    world = fold([*seed_events(), gone])
    assert "w-sofia" not in world.workers
    van_gone = VanRemoved(
        event_id="e-rmv",
        occurred_at=_at(0, 8),
        recorded_at=_at(0, 8),
        dispatch_id="t",
        van_id="van-4",
    )
    assert "van-4" not in fold([*seed_events(), van_gone]).vans


def test_an_extension_makes_impossible_evening_hours_bookable():
    """Three hours of residential work starting at five: beyond everyone's standard
    reach (Dan caps at 7 PM), so no slot exists - until Dan says yes to 8:30."""
    from krama.domain.enums import Certification, ServiceType
    from krama.domain.models import GlassSpec, Job, TimeWindow
    from krama.domain.state import WorldState
    from krama.scheduler.booking import BookingOptions, suggest_booking_slots
    from krama.scheduler.day_planner import SolveParams
    from krama.scheduler.travel.cache import CachingTravelProvider
    from krama.scheduler.travel.synthetic import SyntheticTravelProvider

    def quote(world: WorldState) -> BookingOptions:
        draft = Job(
            id="draft",
            customer_id="c-d",
            customer_name="Evening",
            location=world.jobs["j-402"].location,
            service_type=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
            glass_spec=GlassSpec(),
            required_certifications=frozenset({Certification.RESIDENTIAL_GLAZING}),
            crew_size=1,
            estimated_duration_min=180,
            revenue=700.0,
            windows=(TimeWindow(start=_at(0, 17), end=_at(0, 21)),),
            requested_at=_at(0, 7),
        )
        return suggest_booking_slots(
            world=world,
            travel=CachingTravelProvider(SyntheticTravelProvider()),
            draft=draft,
            horizon=[WEEK_START],
            params=SolveParams(business_tz=BUSINESS_TZ, max_solve_seconds=20.0),
            business=__import__("krama.config", fromlist=["BusinessParams"]).BusinessParams.load(),
            earliest_hour=17,
        )

    without = quote(fold(seed_events()))
    assert without.slots == (), "17:00 + 180min is past every standard reach"

    # To 21:00, not 20:00: the agreement must cover the ride back to the shop,
    # which the solver counts as shift time. The suggestion endpoint adds that
    # margin for exactly this reason.
    with_yes = quote(fold([*seed_events(), _extend("w-dan", 21, "e-y")]))
    assert with_yes.slots, "Dan's yes makes the evening real"
    slot = with_yes.slots[0]
    assert "Dan" in slot.worker_names
    assert slot.overtime_minutes >= 180, "every extended minute is paid overtime"


def test_a_booked_extended_evening_survives_the_weekly_plan():
    """The quote said yes, the booking confirmed - then the HORIZON stage dropped the
    job as "no day had enough crew-hours", because its capacity arithmetic did not
    know about the agreement. Every layer must see the same yes."""
    from krama.config import BusinessParams
    from krama.domain.enums import Certification, CommitmentState, ServiceType
    from krama.domain.models import GlassSpec, Job, TimeWindow
    from krama.scheduler.day_planner import SolveParams
    from krama.scheduler.horizon import HorizonParams, plan_horizon
    from krama.scheduler.travel.cache import CachingTravelProvider
    from krama.scheduler.travel.synthetic import SyntheticTravelProvider

    business = BusinessParams.load()
    world = fold(
        [
            *seed_events(),
            _extend("w-marcus", 21, "e-m"),
            _extend("w-priya", 21, "e-p"),
        ]
    )
    world.jobs["j-evening"] = Job(
        id="j-evening",
        customer_id="c-ev",
        customer_name="Jimmy",
        location=world.jobs["j-401"].location,
        service_type=ServiceType.STOREFRONT_GLASS,
        glass_spec=GlassSpec(),
        required_certifications=frozenset({Certification.COMMERCIAL_STOREFRONT}),
        crew_size=2,
        estimated_duration_min=180,
        revenue=0.0,
        windows=(TimeWindow(start=_at(0, 17), end=_at(0, 20, 45)),),
        commitment_state=CommitmentState.CONFIRMED,
        requested_at=_at(0, 8),
    )
    result = plan_horizon(
        world=world,
        travel=CachingTravelProvider(SyntheticTravelProvider()),
        start=WEEK_START,
        params=SolveParams(business_tz=BUSINESS_TZ, max_solve_seconds=30.0),
        horizon_params=HorizonParams.from_business(business),
    )
    placed = next(
        (
            (r.worker_ids, s.arrival)
            for r in result.routes
            for s in r.stops
            if s.job_id == "j-evening"
        ),
        None,
    )
    assert placed is not None, [
        (u.job_id, u.reason, u.detail) for u in result.unserved if u.job_id == "j-evening"
    ]
    crew, _arrival = placed
    assert set(crew) == {"w-marcus", "w-priya"}
