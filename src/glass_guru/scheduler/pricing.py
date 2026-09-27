"""What to charge, and why.

Everything else in this package computes *cost*: what it takes this business to put a
crew in front of a customer. Nothing until now computed a *price*. The board showed a
dispatcher "+$151.43" and left them to work out whether that was a bill, and it was
neither - it was the extra driving and wages that serving the job adds to the week.

The two numbers answer different questions and both are worth having on screen:

    quote            what the customer pays
    cost to serve    what it costs us to get there and do it
    margin           the difference, which is the number that keeps the lights on

Deliberately arithmetic rather than judgement. A price built from a rate card can be
read down a phone line - "two hours at ninety-five, plus the glass, plus the call-out"
- and argued with. A price a model produced could not be either.

The rate card lives in ``business_params.yaml`` with every other invented number, so
it is visible and arguable rather than buried here as a default argument.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from glass_guru.config import BusinessParams
from glass_guru.domain.catalog import CATALOG
from glass_guru.domain.enums import Priority
from glass_guru.domain.models import Job


@dataclass(frozen=True, slots=True)
class Quote:
    """A price, itemised the way an invoice would be."""

    call_out: float
    labour: float
    materials: float
    uplift: float
    subtotal: float
    tax: float
    total: float

    #: What this job costs us in total: the glass at our cost, the wages for the time
    #: on site, and the driving this particular placement adds to the week. Not part of
    #: the price; shown beside it because a quote without a cost cannot be judged.
    cost_to_serve: float = 0.0
    #: The three parts of it, because "why is the margin thin" is the next question.
    materials_at_cost: float = 0.0
    wages: float = 0.0
    driving: float = 0.0

    #: How the labour line was reached, for saying out loud.
    person_hours: float = 0.0
    crew_size: int = 1
    hit_minimum: bool = False

    @property
    def margin(self) -> float:
        """Subtotal less what the job actually costs. Tax is not ours to keep.

        The first version counted only the driving, which made every quote look like a
        90% margin: the glass and the fitters' wages - by far the two largest real
        costs - were simply missing from the comparison.
        """
        return self.subtotal - self.cost_to_serve

    @property
    def margin_pct(self) -> float:
        return 0.0 if self.subtotal <= 0 else self.margin / self.subtotal * 100

    def explain(self) -> list[str]:
        """The quote as a dispatcher would read it out, line by line."""
        hours = f"{self.person_hours:g}"
        crew = "" if self.crew_size == 1 else f" ({self.crew_size} fitters)"
        lines = [
            f"{'call-out':<30}${self.call_out:>8,.2f}",
            f"{('labour ' + hours + 'h' + crew):<30}${self.labour:>8,.2f}",
        ]
        if self.materials:
            lines.append(f"{'glass and materials':<30}${self.materials:>8,.2f}")
        if self.uplift:
            lines.append(f"{'emergency uplift':<30}${self.uplift:>8,.2f}")
        if self.hit_minimum:
            lines.append("(raised to the minimum charge)")
        lines += [
            f"{'subtotal':<30}${self.subtotal:>8,.2f}",
            f"{'tax':<30}${self.tax:>8,.2f}",
            f"{'TOTAL':<30}${self.total:>8,.2f}",
        ]
        return lines


def quote_for(job: Job, business: BusinessParams, driving_cost: float = 0.0) -> Quote:
    """Price one job from the rate card.

    Labour is billed per fitter, rounded up to the quarter hour - a crew of two on a
    ninety-minute job is three billable hours, which is the thing customers query most
    and the thing a quote has to be able to defend.
    """
    card = business.pricing
    rate = float(card.labour_rate_per_hour.value)
    markup = float(card.materials_markup.value)

    billable_hours = math.ceil(job.estimated_duration_min / 15) * 0.25
    person_hours = billable_hours * job.crew_size
    labour = round(person_hours * rate, 2)

    entry = CATALOG.get(job.service_type)
    panes = max(0, job.glass_spec.pane_count - 1) if job.glass_spec else 0
    materials_cost = 0.0
    if entry is not None:
        materials_cost = entry.materials_cost + entry.materials_cost_per_extra_pane * panes
    materials = round(materials_cost * markup, 2)

    call_out = float(card.call_out_fee.value)
    base = call_out + labour + materials

    # An emergency is not a surcharge on the glass, it is a surcharge on being
    # interrupted, so it scales the whole job rather than one line.
    uplift = 0.0
    if job.priority is Priority.EMERGENCY:
        uplift = round(base * (float(card.emergency_uplift.value) - 1.0), 2)

    subtotal = round(base + uplift, 2)
    minimum = float(card.minimum_charge.value)
    hit_minimum = subtotal < minimum
    if hit_minimum:
        subtotal = minimum

    # What it really costs: the glass at our cost, the wages for the time on site, and
    # whatever driving this placement adds. Anything less makes the margin fiction.
    wages = round(person_hours * 60 * float(business.labor.loaded_rate_per_minute.value), 2)
    total_cost = round(materials_cost + wages + driving_cost, 2)

    tax = round(subtotal * float(card.tax_rate.value), 2)
    return Quote(
        call_out=call_out,
        labour=labour,
        materials=materials,
        uplift=uplift,
        subtotal=subtotal,
        tax=tax,
        total=round(subtotal + tax, 2),
        cost_to_serve=total_cost,
        materials_at_cost=round(materials_cost, 2),
        wages=wages,
        driving=round(driving_cost, 2),
        person_hours=person_hours,
        crew_size=job.crew_size,
        hit_minimum=hit_minimum,
    )
