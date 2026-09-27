"""What to charge, as opposed to what it costs.

Every other money figure in this system is a cost. These are the first prices, and
the thing worth protecting is that the two never get mixed up: a quote that quietly
counted only the driving as cost showed a 98% margin on every job, which would have
been believed for exactly as long as it took somebody to look at a bank statement.
"""

from __future__ import annotations

import pytest

from glass_guru.config import BusinessParams
from glass_guru.domain.enums import Priority, ServiceType
from glass_guru.domain.models import GlassSpec, Job, Location
from glass_guru.fixtures.sample_business import _at
from glass_guru.scheduler.pricing import quote_for

WHEN = _at(0, 8)


@pytest.fixture
def business() -> BusinessParams:
    return BusinessParams.load()


def job(
    service: ServiceType = ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
    *,
    minutes: int = 120,
    crew: int = 1,
    panes: int = 1,
    priority: Priority = Priority.NORMAL,
) -> Job:
    return Job(
        id="draft",
        customer_id="c",
        customer_name="Test",
        location=Location(lat=33.0, lon=-97.3, address="somewhere"),
        service_type=service,
        glass_spec=GlassSpec(pane_count=panes),
        crew_size=crew,
        estimated_duration_min=minutes,
        priority=priority,
        requested_at=WHEN,
    )


def test_labour_is_billed_per_fitter(business: BusinessParams):
    """Two people for ninety minutes is three billable hours. This is the line
    customers query most and the one a quote has to be able to defend."""
    one = quote_for(job(minutes=90, crew=1), business)
    two = quote_for(job(minutes=90, crew=2), business)
    assert two.labour == pytest.approx(one.labour * 2)
    assert two.person_hours == 3.0


def test_time_rounds_up_to_the_quarter_hour(business: BusinessParams):
    """Nobody bills 47 minutes."""
    assert quote_for(job(minutes=45), business).person_hours == 0.75
    assert quote_for(job(minutes=46), business).person_hours == 1.0
    assert quote_for(job(minutes=60), business).person_hours == 1.0
    assert quote_for(job(minutes=61), business).person_hours == 1.25


def test_a_small_job_is_raised_to_the_minimum(business: BusinessParams):
    """A screen repair priced honestly by the hour comes to less than the fuel."""
    small = quote_for(job(ServiceType.SCREEN_REPAIR, minutes=30), business)
    assert small.hit_minimum
    assert small.subtotal == business.pricing.minimum_charge.value


def test_a_large_job_is_not(business: BusinessParams):
    big = quote_for(job(ServiceType.STOREFRONT_GLASS, minutes=180, crew=2), business)
    assert not big.hit_minimum
    assert big.subtotal > business.pricing.minimum_charge.value


def test_extra_panes_cost_more(business: BusinessParams):
    one = quote_for(job(panes=1), business)
    three = quote_for(job(panes=3), business)
    assert three.materials > one.materials


def test_an_emergency_scales_the_whole_job(business: BusinessParams):
    """The surcharge is for being interrupted, not for the glass, so it applies to
    everything rather than to one line."""
    normal = quote_for(job(ServiceType.EMERGENCY_BOARD_UP, minutes=60), business)
    urgent = quote_for(
        job(ServiceType.EMERGENCY_BOARD_UP, minutes=60, priority=Priority.EMERGENCY), business
    )
    assert urgent.uplift > 0 and normal.uplift == 0
    assert urgent.subtotal > normal.subtotal


def test_margin_counts_the_glass_and_the_wages(business: BusinessParams):
    """The bug this file exists to prevent.

    Counting only the driving made every quote look like a 90% margin, because the two
    largest real costs - the glass and the fitters' time - were missing from the
    comparison entirely.
    """
    quoted = quote_for(job(ServiceType.STOREFRONT_GLASS, minutes=150, crew=2), business, 45.0)
    assert quoted.materials_at_cost > 0
    assert quoted.wages > 0
    assert quoted.driving == 45.0
    assert quoted.cost_to_serve == pytest.approx(
        quoted.materials_at_cost + quoted.wages + quoted.driving
    )
    # A glazing trade runs somewhere in this band. Outside it, something is wrong with
    # the rate card rather than with the arithmetic.
    assert 15 < quoted.margin_pct < 60


def test_driving_further_eats_the_margin(business: BusinessParams):
    """Which is the whole reason the slots are ranked."""
    near = quote_for(job(), business, driving_cost=5.0)
    far = quote_for(job(), business, driving_cost=95.0)
    assert near.total == far.total, "the customer pays the same wherever they are"
    assert far.margin == pytest.approx(near.margin - 90.0)


def test_tax_is_shown_separately(business: BusinessParams):
    quoted = quote_for(job(), business)
    assert quoted.total == pytest.approx(quoted.subtotal + quoted.tax)
    assert quoted.margin < quoted.subtotal, "tax is not ours to keep"


def test_the_quote_can_be_read_down_a_phone(business: BusinessParams):
    lines = quote_for(job(minutes=120, crew=2), business).explain()
    text = "\n".join(lines)
    assert "call-out" in text and "labour 4h (2 fitters)" in text
    assert "TOTAL" in text
