"""Four things a dispatcher found by using the board, and what stops them returning.

Every one of these looked like the system working. A price appeared, a slot appeared,
a button was pressed. What made them bugs is that the answer had nothing to do with
what the caller actually said.
"""

from __future__ import annotations

import pytest

from glass_guru.agents.intake import (
    CallExtraction,
    _distrust_a_size_read_as_a_count,
    _fix_the_direction,
)

# ------------------------------------------------- a town is not an address


def test_a_town_is_refused_as_an_address():
    """ "Haslet" matches an administrative boundary covering the whole town, and
    quoting against its centroid is a real-looking price for a journey to nowhere in
    particular."""
    from glass_guru.geocoding import AddressTooVague, Geocoder

    with pytest.raises(AddressTooVague):
        Geocoder._refuse_an_area("Haslet", {"precision": "area", "display_name": "Haslet, Texas"})


def test_a_street_or_a_house_is_accepted():
    from glass_guru.geocoding import Geocoder

    Geocoder._refuse_an_area("2nd Ave", {"precision": "road"})
    Geocoder._refuse_an_area("420 Main St", {"precision": "house"})


# --------------------------------------------- a measurement is not a count


@pytest.mark.parametrize(
    ("text", "extracted"),
    [
        ("about a 6 ft glass, for door", 6),
        ("a 3 by 4 pane in the kitchen", 12),
        ("a 3' x 4' unit", 12),
        ("a 900mm panel", 900),
    ],
)
def test_a_size_is_not_a_pane_count(text: str, extracted: int):
    """ "6 ft glass" became six panes, which took a two-hour job to five and three
    quarter hours and put two fitters on it - a day's work the caller never asked for,
    quoted at a day's price."""
    assert (
        _distrust_a_size_read_as_a_count(CallExtraction(pane_count=extracted), text).pane_count == 1
    )


@pytest.mark.parametrize(
    ("text", "extracted"),
    [
        ("six panes need doing across the front", 6),
        ("two windows went in the front room", 2),
        ("six panes, each 3 by 4", 6),
    ],
)
def test_a_real_count_survives(text: str, extracted: int):
    """A caller who counts out loud is believed, measurement or not. Clipping a genuine
    six-pane job to one would be the worse bug."""
    got = _distrust_a_size_read_as_a_count(CallExtraction(pane_count=extracted), text)
    assert got.pane_count == extracted


# ------------------------------------------- which side of the range they meant


@pytest.mark.parametrize(
    ("text", "earliest", "latest"),
    [
        ("Free anytime of the week after 4pm", 16, None),
        ("from 10am please", 10, None),
        ("not before 8", 8, None),
        ("no earlier than 2pm", 14, None),
        ("after 4pm but before 7", 16, 19),
        ("any time that suits", None, None),
    ],
)
def test_a_stated_bound_lands_on_the_side_it_was_said(
    text: str, earliest: int | None, latest: int | None
):
    """ "Free after 4pm" came back as latest_hour 16 - finish by four - and every slot
    offered was six in the morning. The exact opposite of the request, which reads as
    the system ignoring the customer entirely.

    Which side of a range a word puts you on is not interpretation, so it is checked.
    """
    fixed = _fix_the_direction(CallExtraction(), text)
    assert (fixed.earliest_hour, fixed.latest_hour) == (earliest, latest)


def test_a_spelled_out_time_is_left_to_the_model():
    """The guard corrects direction where there is a number to correct. "before we
    open at nine" has no digit, so the model's own answer stands - which is right, and
    is why the guard adjusts rather than replaces."""
    stated = CallExtraction(latest_hour=9)
    fixed = _fix_the_direction(stated, "must be done before we open at nine")
    assert (fixed.earliest_hour, fixed.latest_hour) == (None, 9)


def test_a_bare_hour_from_a_customer_means_the_working_day():
    """ "After 4" means the afternoon. Nobody is asking a glazier to come at four in
    the morning, which is why they did not bother saying pm."""
    assert _fix_the_direction(CallExtraction(), "after 4").earliest_hour == 16
    assert _fix_the_direction(CallExtraction(), "after 10").earliest_hour == 10


def test_a_contradictory_pair_drops_the_upper_bound():
    """Work that starts after four and finishes by four is not a constraint, it is a
    refusal with extra steps."""
    assert CallExtraction(earliest_hour=16, latest_hour=16).latest_hour is None


# ------------------------------------------------------- paying for overtime


def test_work_past_the_shift_is_charged_at_the_rate_it_is_paid():
    """A four-o'clock start on a two-hour job keeps a fitter an hour late. We pay time
    and a half for that hour; if we do not bill it, the margin quietly absorbs it."""
    from glass_guru.config import BusinessParams
    from glass_guru.scheduler.pricing import quote_for
    from tests.unit.test_pricing import job

    business = BusinessParams.load()
    inside = quote_for(job(minutes=120), business, driving_cost=20.0)
    late = quote_for(job(minutes=120), business, driving_cost=20.0, overtime_minutes=60)

    assert late.labour > inside.labour, "after-hours labour bills more"
    assert late.wages > inside.wages, "and costs more"
    # The margin percentage holds: we charge the premium we pay, rather than eating it.
    assert late.margin_pct == pytest.approx(inside.margin_pct, abs=1.0)


# ------------------------------------------------ words the caller never said


def test_an_invented_commitment_is_not_priced():
    """ "fixed on monday morning" came back with time_off_work and the quote "time off
    work" - words James never said. The model was asked to price goodwill and invented
    the receipt. Every quote must appear in the transcript."""
    from glass_guru.agents.intake import CommitmentSignal, grounded_commitments

    call = CallExtraction(
        commitment_signals=[CommitmentSignal.TIME_OFF_WORK],
        commitment_quotes=["time off work"],
    )
    grounded = grounded_commitments(call, "james needs the window fixed on monday morning")
    assert grounded.commitment_signals == []
    assert grounded.commitment_quotes == []


def test_a_commitment_the_caller_voiced_survives_punctuation():
    from glass_guru.agents.intake import CommitmentSignal, grounded_commitments

    call = CallExtraction(
        commitment_signals=[CommitmentSignal.TIME_OFF_WORK],
        commitment_quotes=["I'd have to take the morning off work"],
    )
    text = "it's Sarah - I'd have to take the morning off work, sadly."
    assert grounded_commitments(call, text).commitment_signals == [CommitmentSignal.TIME_OFF_WORK]


def test_a_street_the_caller_never_said_is_challenged():
    """ "16 Haslet, Texas" resolves cleanly to 16 Avondale Haslet Road - a real front
    door nobody asked for a van at. Nominatim is helpful to a fault."""
    from glass_guru.agents.intake import _the_map_guessed
    from glass_guru.domain.models import Location

    guessed = _the_map_guessed(
        "16 Haslet, Texas", Location(lat=33.0, lon=-97.3, matched_road="Avondale Haslet Road")
    )
    assert guessed == "Avondale Haslet Road"


def test_a_street_the_caller_did_say_is_not():
    from glass_guru.agents.intake import _the_map_guessed
    from glass_guru.domain.models import Location

    spoken = "300 W Byron Nelson Blvd, Roanoke TX"
    matched = Location(lat=33.0, lon=-97.2, matched_road="West Byron Nelson Boulevard")
    assert _the_map_guessed(spoken, matched) == ""


def test_nobody_is_offered_the_crack_of_dawn_they_did_not_ask_for():
    """ "The morning" made the model guess earliest_hour 6, and the search offered
    every customer the start of Marcus's shift. Below eight stands only when the
    caller typed the early hour themselves."""
    from glass_guru.agents.intake import _civil_floor

    assert _civil_floor(CallExtraction(earliest_hour=6), "the morning please").earliest_hour == 8
    assert _civil_floor(CallExtraction(earliest_hour=6), "after 6am works").earliest_hour == 6
    assert _civil_floor(CallExtraction(earliest_hour=16), "after 4pm").earliest_hour == 16
