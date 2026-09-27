"""Intake: a phone call, turned into a job the engine can price.

The headline feature, and the clearest case for having an agent at all. A caller says
"my front window's smashed, I'm around Tuesday but I'd have to take the morning off" -
and three quite different things need to happen to that sentence.

**Classify.** Which service is this? A model does that well.

**Estimate.** How long will it take, what certifications, what parts? A model does
that *badly*, so it does not: :mod:`glass_guru.domain.catalog` answers from the
service type. A schedule built on an invented duration is wrong in a way no invariant
check can catch, because every arrival time is internally consistent with the fiction.

**Price the promise.** "I'd have to take the morning off" means this slot is expensive
to move. That is the single clearest justification for a model in this pipeline - no
dropdown captures it, and the solver has no way to infer it. But the model only
identifies *which* signal was expressed, from a fixed list. The dollar figure comes
from configuration, because asking a language model to price goodwill produces a
confident number with nothing behind it.

What is missing matters as much as what was captured. A dispatcher mid-call needs
"ask for a callback number" while the customer is still on the line, not a silently
half-filled form discovered later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.structured import Example, Extraction, extract
from glass_guru.config import BusinessParams
from glass_guru.domain.catalog import Estimate, describe_for_prompt, lookup
from glass_guru.domain.enums import GlassType, Priority, PropertyType, ServiceType
from glass_guru.domain.models import GlassSpec, Job, Location, Material, Provenance
from glass_guru.geocoding import GeocodeError, Geocoder, for_service_area
from glass_guru.obs.tracing import record, span


class CommitmentSignal(StrEnum):
    """What a caller said they had arranged around this appointment.

    A closed list on purpose. The model recognises which of these was expressed; the
    cost of each is a business parameter, not the model's to invent.
    """

    TIME_OFF_WORK = "time_off_work"
    WAITING_IN = "waiting_in"
    ARRANGED_CHILDCARE = "arranged_childcare"
    BUSINESS_CLOSED = "business_closed"
    ALREADY_RESCHEDULED = "already_rescheduled"


#: Things a model writes when it means "nothing".
#:
#: Asked for a field it was not told, a model would rather answer than leave a blank,
#: so it fills in "N/A" or "unknown" - and a string like that is perfectly truthy. A
#: caller who said "callback" and then never gave the number came back with phone set
#: to "N/A", which counted as answered: not in the missing list, not in "still to ask",
#: shown on screen as a value. The dispatcher hangs up without the number.
#:
#: Normalised on the model rather than at the one place that noticed, because every
#: consumer downstream has the same problem and none of them should have to know.
_NOT_AN_ANSWER = frozenset(
    {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "unspecified",
        "not given",
        "not provided",
        "not stated",
        "not available",
        "missing",
        "tbd",
        "tba",
        "?",
    }
)


def stated(value: str) -> str:
    """The value, or empty if the model was really saying it did not know."""
    return "" if value.strip().lower().strip(".") in _NOT_AN_ANSWER else value.strip()


#: The fewest digits that could be a phone number. Seven is a local US number; ten
#: with an area code. Low on purpose - the job is to reject "Nguyen Glass", not to
#: adjudicate international dialling plans.
_PHONE_DIGITS = 7


def phone_or_blank(value: str) -> str:
    """The value if it could be a phone number, otherwise nothing.

    Blanking "N/A" was not enough. Told to produce a phone and given no number, the
    model does not give up - it reaches for the nearest string and writes the company
    name in. The form then reads Phone: Nguyen Glass, which is worse than N/A, because
    it looks like data.

    A phone number is checkable, so it gets checked rather than trusted. That is the
    same line the whole system is drawn on: the model reads the call, and anything
    with a verifiable shape is verified.
    """
    if sum(character.isdigit() for character in value) < _PHONE_DIGITS:
        return ""
    return value


def email_or_blank(value: str) -> str:
    """Same argument, same reason. An address without an @ is not one."""
    local, _, domain = value.partition("@")
    return value if local and "." in domain else ""


class CallExtraction(BaseModel):
    """What the model heard. Nothing here is a scheduling decision."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _blank_out_non_answers(cls, value: object) -> object:
        return stated(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _a_window_must_be_possible(self) -> CallExtraction:
        """Drop an upper bound that contradicts the lower one.

        "Free after 4pm" came back as earliest 16 *and* latest 16, which asks for work
        that starts after four and finishes by four. A model reaching for both ends of
        a range it was given one end of is a predictable slip, and it is checkable, so
        it is checked rather than prompted away.
        """
        if (
            self.earliest_hour is not None
            and self.latest_hour is not None
            and self.latest_hour <= self.earliest_hour
        ):
            object.__setattr__(self, "latest_hour", None)
        return self

    @field_validator("phone", mode="after")
    @classmethod
    def _only_something_that_could_be_dialled(cls, value: str) -> str:
        return phone_or_blank(value)

    @field_validator("email", mode="after")
    @classmethod
    def _only_something_that_could_be_emailed(cls, value: str) -> str:
        return email_or_blank(value)

    customer_name: str = Field(default="", description="The caller's name, if given.")
    phone: str = Field(default="", description="Callback number, digits as spoken.")
    email: str = Field(default="")
    address: str = Field(default="", description="The address as the caller said it.")

    service_type: str = Field(
        default="",
        description="One service type from the catalogue. Empty if genuinely unclear.",
    )
    description: str = Field(default="", description="What they said is wrong, briefly.")
    pane_count: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How many panes of glass, only if the caller counted them. A measurement "
            "is not a count: 'a 6 ft glass' is one pane, not six."
        ),
    )
    glass_type: str = Field(
        default="", description="annealed, tempered, laminated or insulated_unit, if known."
    )
    property_type: Literal["residential", "commercial", "vehicle", ""] = ""

    urgency: Literal["emergency", "high", "normal", "low"] = Field(
        default="normal",
        description="emergency only when the property is insecure or unsafe right now.",
    )
    preferred_timing: str = Field(
        default="", description='In their words, e.g. "Tuesday afternoon", "any morning".'
    )
    # The same preference as a number, because prose cannot constrain a search.
    # "Free anytime after 4pm" was extracted perfectly into preferred_timing and then
    # ignored by everything downstream, so every slot came back at six in the morning.
    earliest_hour: int | None = Field(
        default=None,
        ge=0,
        le=23,
        description=(
            "Earliest hour they will accept, 24-hour. 16 for 'after 4pm', "
            "12 for 'afternoons'. Null if they did not say."
        ),
    )
    latest_hour: int | None = Field(
        default=None,
        ge=0,
        le=23,
        description=(
            "Latest hour the work may still be going on, 24-hour. 12 for 'mornings "
            "only', 9 for 'before we open at nine'. Null if they did not say."
        ),
    )
    hard_constraint: str = Field(
        default="",
        description=(
            'A constraint that cannot be broken, e.g. "must be finished before we open '
            'at nine". Empty when there is none.'
        ),
    )
    customer_must_be_present: bool | None = None

    commitment_signals: list[CommitmentSignal] = Field(
        default_factory=list,
        description=(
            "Which of the listed arrangements the caller said they had made around "
            "this appointment. Only what was actually said."
        ),
    )
    commitment_quotes: list[str] = Field(
        default_factory=list,
        description="The caller's own words for each signal, verbatim and short.",
    )

    site_notes: str = Field(
        default="",
        description='Access details worth writing down: gate codes, parking, "alley only".',
    )
    details_covered: list[str] = Field(
        default_factory=list,
        description="Which of the catalogue's ask-about items the caller already answered.",
    )


SYSTEM = """\
You are helping a dispatcher take a call at a glass-fitting business. Turn what the
caller said into structured details.

Rules:
- Record only what was said. Never infer an address, a phone number, or a time.
- Pick one service type from the catalogue below, or leave it empty if genuinely unclear.
- Do not estimate how long the job will take. That is not your job and the system
  already knows.
- pane_count is how many panes, not how big. "a 6ft glass", "a 3 by 4 pane" and "the
  big window" are all one pane. Six panes means the caller said six. Getting this
  wrong triples the duration and the crew, and the customer is quoted for a day's work
  they did not ask for.
- Put any time preference in numbers as well as words. "Free after 4pm" is
  earliest_hour 16. "Mornings only" is latest_hour 12. "Before we open at nine" is
  latest_hour 9. Set only the end the caller actually gave: "after 4pm" has an
  earliest and no latest; "before nine" has a latest and no earliest. Leave them null
  when the caller did not say - a guessed constraint is worse than none, because it
  silently rules out slots they would have taken.
- commitment_signals: only arrangements the caller actually mentioned making. Quote
  their words. Say nothing if they said nothing. The difference between these matters,
  because they are priced very differently:
    time_off_work      - they are giving up work or leave for this. "I'd have to take
                         the morning off", "I'm booking a day's holiday".
    waiting_in         - they will simply be at home anyway. "I'll be in all day",
                         "I'm around Friday whenever". Much weaker than taking leave.
    arranged_childcare - cover has been booked that would have to be rebooked.
    business_closed    - a business is closing, opening late, or losing trade for us.
    already_rescheduled - we have moved this appointment before.
- urgency is `emergency` only when the property is insecure or someone is unsafe now.

Catalogue:
{catalogue}
"""


EXAMPLES: tuple[Example, ...] = (
    # A size, not a count. "6 ft" became six panes, which tripled the duration to five
    # and three quarter hours and put two fitters on it - a day's work the caller never
    # asked for, quoted at a day's price. A rule alone did not fix it; a worked example
    # did, which is the documented lever for a small model.
    Example(
        text=(
            "Ani here, 913-295-2960. Backyard door glass is broken at 300 W Byron "
            "Nelson Blvd, Roanoke. About a 6 ft glass, ground floor. Free after 4pm."
        ),
        output=CallExtraction(
            customer_name="Ani",
            phone="913-295-2960",
            address="300 W Byron Nelson Blvd, Roanoke",
            service_type="residential_window_replacement",
            description="backyard door glass broken, about 6 ft",
            pane_count=1,
            property_type="residential",
            preferred_timing="after 4pm",
            earliest_hour=16,
            site_notes="ground floor",
        ),
    ),
    Example(
        text=(
            "Hi, it's Sarah Chen, 206-555-0142. Two windows went in the front room, "
            "4410 Ballard Ave. I can do Tuesday but I'd have to take the morning off work."
        ),
        output=CallExtraction(
            customer_name="Sarah Chen",
            phone="206-555-0142",
            address="4410 Ballard Ave",
            service_type="residential_window_replacement",
            description="Two front-room windows broken",
            pane_count=2,
            property_type="residential",
            preferred_timing="Tuesday morning",
            latest_hour=12,
            commitment_signals=[CommitmentSignal.TIME_OFF_WORK],
            commitment_quotes=["I'd have to take the morning off work"],
            details_covered=["how many panes"],
        ),
    ),
    Example(
        text="Someone's put the shop window through overnight. We open at nine.",
        output=CallExtraction(
            description="Storefront window smashed overnight",
            service_type="storefront_glass",
            property_type="commercial",
            urgency="emergency",
            hard_constraint="must be finished before the shop opens at 09:00",
            commitment_signals=[CommitmentSignal.BUSINESS_CLOSED],
            commitment_quotes=["We open at nine"],
        ),
    ),
)


#: What a job cannot be scheduled without. Everything else can follow on a second call.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("customer_name", "the customer's name"),
    ("phone", "a callback number"),
    ("address", "the address"),
    ("service_type", "what kind of work it is"),
)


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """A draft job, what it will cost to move, and what is still missing."""

    draft: Job | None
    extraction: Extraction[CallExtraction]
    estimate: Estimate | None
    commitment_cost: float
    commitment_quotes: tuple[str, ...]
    #: Things a dispatcher should ask before hanging up.
    ask_next: tuple[str, ...]
    missing_required: tuple[str, ...]
    #: The caller's stated hours, as numbers the search can use.
    earliest_hour: int | None = None
    latest_hour: int | None = None
    geocode_note: str = ""

    @property
    def bookable(self) -> bool:
        """Whether there is enough to price this into the schedule."""
        return self.draft is not None and not self.missing_required

    @property
    def call(self) -> CallExtraction | None:
        return self.extraction.value


def commitment_cost_for(signals: list[CommitmentSignal], business: BusinessParams) -> float:
    """Turn recognised signals into a number, capped.

    Deliberately not the model's decision. The cap matters too: without it a caller
    listing every inconvenience could make one job effectively immovable and distort
    the whole week around it.
    """
    weights = {
        CommitmentSignal.TIME_OFF_WORK: business.commitment.time_off_work.value,
        CommitmentSignal.WAITING_IN: business.commitment.waiting_in.value,
        CommitmentSignal.ARRANGED_CHILDCARE: business.commitment.arranged_childcare.value,
        CommitmentSignal.BUSINESS_CLOSED: business.commitment.business_closed.value,
        CommitmentSignal.ALREADY_RESCHEDULED: business.commitment.already_rescheduled.value,
    }
    total = sum(weights.get(signal, 0.0) for signal in set(signals))
    return min(total, business.commitment.max_total.value)


#: A measurement, however it is written: a pair like "3 by 4" or "3' x 4\"", or a
#: single one like "6 ft". Both are sizes, and neither is a quantity of panes.
_DIMENSIONS = re.compile(
    r"\b\d+\s*(?:'|ft|foot|feet|\"|in|inch(?:es)?|cm|m|mm)?\s*(?:x|by|\*)\s*\d+"
    r"|\b\d+\s*(?:'|ft|foot|feet|\"|inch(?:es)?|cm|mm)\b",
    re.IGNORECASE,
)
#: An explicit count: "six panes", "2 windows", "three sheets".
_COUNTED = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:pane|window|sheet|unit|glass)e?s\b",
    re.IGNORECASE,
)


#: "after 4pm", "from 4", "not before 4" - a lower bound on when we may turn up.
_AFTER = re.compile(
    r"\b(?:after|from|not before|no earlier than|past)\s+(\d{1,2})\s*(?::\s*(\d{2}))?\s*"
    r"(am|pm|o'?clock)?",
    re.IGNORECASE,
)
#: "before 9", "by 9", "until 9" - an upper bound.
_BEFORE = re.compile(
    # "not before 8" is a lower bound wearing an upper bound's words, so the negated
    # forms are excluded here and caught by _AFTER instead.
    r"(?<!not )(?<!no )(?<!earlier than )"
    r"\b(?:before|by|until|till|til|no later than)\s+(\d{1,2})\s*"
    r"(?::\s*(\d{2}))?\s*(am|pm|o'?clock)?",
    re.IGNORECASE,
)


def _hour_24(digits: str, meridiem: str | None) -> int | None:
    hour = int(digits)
    if hour > 23:
        return None
    marker = (meridiem or "").lower()
    if marker.startswith("pm") and hour < 12:
        hour += 12
    elif marker.startswith("am") and hour == 12:
        hour = 0
    elif not marker and hour <= 7:
        # "after 4" from a customer talking about their day means the afternoon. A
        # glass fitter is not turning up at four in the morning, and the caller knows
        # that, which is why they did not bother saying pm.
        hour += 12
    return hour


def _fix_the_direction(call: CallExtraction, text: str) -> CallExtraction:
    """Put a stated bound on the side the caller actually put it.

    "Free anytime of the week after 4pm" came back as latest_hour 16 - finish by four -
    and every slot offered was six in the morning, which is the exact opposite of what
    was asked for and reads as the system ignoring the customer entirely.

    Which side of a range a word puts you on is not a matter of interpretation, so it
    is checked. The model still does the hard part: finding the time in the prose.
    """
    after = _AFTER.search(text)
    before = _BEFORE.search(text)
    update: dict[str, int | None] = {}

    if after:
        hour = _hour_24(after.group(1), after.group(3))
        if hour is not None:
            update["earliest_hour"] = hour
            if call.latest_hour is not None and call.latest_hour <= hour and not before:
                update["latest_hour"] = None
    if before:
        hour = _hour_24(before.group(1), before.group(3))
        if hour is not None:
            update["latest_hour"] = hour

    return call.model_copy(update=update) if update else call


def _distrust_a_size_read_as_a_count(call: CallExtraction, text: str) -> CallExtraction:
    """A measurement is not a quantity.

    "About a 6 ft glass" came back as six panes, which took a two-hour job to five and
    three quarter hours and put two fitters on it - a day's work the caller never asked
    for, at a day's price. A worked example fixed that phrasing; "a 3 by 4 pane" still
    multiplied the dimensions.

    So it is checked rather than prompted. If the caller wrote a measurement and never
    wrote a count, the count is one. Both halves matter: "six panes, each 3 by 4" says
    a number out loud and keeps it.
    """
    if call.pane_count is None or call.pane_count <= 1:
        return call
    if _DIMENSIONS.search(text) and not _COUNTED.search(text):
        return call.model_copy(update={"pane_count": 1})
    return call


def intake(
    provider: LLMProvider,
    text: str,
    *,
    business: BusinessParams,
    now: datetime,
    geocoder: Geocoder | None = None,
    job_id: str = "draft",
    max_attempts: int = 3,
) -> IntakeResult:
    """Turn call notes into a draft job, or say what is still needed."""
    with span("agent.intake", chars=len(text)) as active:
        extraction = extract(
            provider,
            CallExtraction,
            system=SYSTEM.format(catalogue=describe_for_prompt()),
            text=text,
            examples=EXAMPLES,
            max_attempts=max_attempts,
            schema_description="Details heard on the call.",
        )

        call = extraction.value
        if call is not None:
            call = _distrust_a_size_read_as_a_count(call, text)
            call = _fix_the_direction(call, text)
        if call is None:
            record(escalated=True, provider_failed=extraction.provider_failed)
            active.set_attribute("outcome", "escalated")
            return IntakeResult(
                draft=None,
                extraction=extraction,
                estimate=None,
                commitment_cost=0.0,
                commitment_quotes=(),
                ask_next=("Could you take those details again by hand?",),
                missing_required=tuple(label for _, label in REQUIRED_FIELDS),
            )

        missing = tuple(
            label for field_name, label in REQUIRED_FIELDS if not getattr(call, field_name)
        )

        estimate: Estimate | None = None
        if call.service_type:
            try:
                service = ServiceType(call.service_type)
            except ValueError:
                # The model named something outside the catalogue. Treat the service
                # type as unknown rather than guessing the nearest match - a wrong
                # service type silently produces a wrong duration and wrong crew.
                missing = (*missing, "what kind of work it is")
            else:
                estimate = lookup(
                    service,
                    pane_count=call.pane_count or 1,
                    glass_type=_glass_type(call.glass_type),
                    known_details=frozenset(call.details_covered),
                )

        commitment = commitment_cost_for(call.commitment_signals, business)

        location: Location | None = None
        geocode_note = ""
        if call.address:
            try:
                location = (geocoder or for_service_area()).geocode(call.address)
            except GeocodeError as exc:
                geocode_note = str(exc)
                missing = (*missing, "an address we can find on the map")

        draft = (
            _build_draft(
                job_id=job_id,
                call=call,
                estimate=estimate,
                location=location,
                commitment=commitment,
                now=now,
                extraction=extraction,
            )
            if estimate is not None and location is not None
            else None
        )

        ask_next = tuple(
            [f"Ask for {label}." for label in missing]
            + list(estimate.missing_details if estimate else ())
        )

        record(
            repairs=extraction.repairs,
            missing=len(missing),
            commitment_cost=commitment,
            bookable=draft is not None and not missing,
            service=call.service_type,
        )
        active.set_attribute("outcome", "bookable" if draft is not None else "incomplete")

        return IntakeResult(
            draft=draft,
            extraction=extraction,
            estimate=estimate,
            commitment_cost=commitment,
            commitment_quotes=tuple(call.commitment_quotes),
            ask_next=ask_next,
            missing_required=missing,
            earliest_hour=call.earliest_hour,
            latest_hour=call.latest_hour,
            geocode_note=geocode_note,
        )


def _glass_type(value: str) -> GlassType | None:
    try:
        return GlassType(value) if value else None
    except ValueError:
        return None


def _build_draft(
    *,
    job_id: str,
    call: CallExtraction,
    estimate: Estimate,
    location: Location,
    commitment: float,
    now: datetime,
    extraction: Extraction[CallExtraction],
) -> Job:
    """Assemble the job. Every scheduling number comes from the catalogue, not the call."""
    materials = tuple(
        Material(
            part_code=part,
            quantity=max(1, call.pane_count or 1),
            in_stock=not estimate.needs_ordering,
            lead_time_days=estimate.lead_time_days,
        )
        for part in estimate.parts
    )
    return Job(
        id=job_id,
        customer_id=f"c-{job_id}",
        customer_name=call.customer_name or "Unnamed caller",
        phone=call.phone,
        email=call.email,
        location=location,
        property_type=PropertyType(call.property_type)
        if call.property_type
        else PropertyType.RESIDENTIAL,
        service_type=estimate.entry.service_type,
        description_raw=call.description,
        glass_spec=GlassSpec(
            pane_count=max(1, call.pane_count or 1),
            glass_type=_glass_type(call.glass_type),
        ),
        required_certifications=estimate.required_certifications,
        crew_size=estimate.crew_size,
        estimated_duration_min=estimate.duration_min,
        duration_confidence_min=estimate.confidence_min,
        materials=materials,
        priority=Priority(call.urgency),
        commitment_cost=commitment,
        requires_customer_present=(
            call.customer_must_be_present
            if call.customer_must_be_present is not None
            else estimate.requires_customer_present
        ),
        site_notes=call.site_notes,
        requested_at=now,
        provenance=Provenance(
            source_channel="phone",
            received_at=now,
            missing_required=(),
            extractor_retries=extraction.repairs,
        ),
    )
