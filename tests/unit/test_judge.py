"""The tone judge, and the check that decides whether to believe it.

Nothing here calls a model. The point of these tests is the machinery around the
judge: that an uncalibrated one is refused, that a judge with only one opinion is
caught, and that the rules which need no opinion are not sent to it in the first
place.
"""

from __future__ import annotations

import pytest

from glass_guru.agents.comms import DraftMessage, verify_house_style
from glass_guru.evals.judge import MIN_AGREEMENT, Calibration, load_labels


def draft(body: str, channel: str = "sms") -> DraftMessage:
    return DraftMessage(job_id="j-402", channel=channel, body=body)


def calibration(agreed: int, total: int, verdicts: set[str]) -> Calibration:
    return Calibration(
        agreed=agreed, total=total, disagreements=(), verdicts_produced=frozenset(verdicts)
    )


# --------------------------------------------------------------- house style


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hi Sarah - we're moving j-402 to Thursday.", "internal identifier"),
        ("Hi - van-3 has broken down, so we're running late.", "internal identifier"),
        ("Your booking is still provisional, we'll confirm shortly.", "internal vocabulary"),
        ("Sorry for the delay - we'll take 10% off as a discount.", "unauthorised compensation"),
        ("We'll do the work free of charge to make up for it.", "unauthorised compensation"),
    ],
)
def test_house_style_catches_what_needs_no_opinion(body: str, expected: str):
    issues = verify_house_style(draft(body))
    assert issues, f"nothing flagged in: {body}"
    assert any(expected in i.detail for i in issues), [i.detail for i in issues]


def test_apologising_once_is_fine_and_twice_is_not():
    assert not verify_house_style(draft("Sorry - we're running late, with you by 3pm."))
    twice = verify_house_style(
        draft("Sorry about this. We do apologise for the inconvenience caused.")
    )
    assert any("more than once" in i.detail for i in twice)


def test_an_sms_the_carrier_would_split_is_flagged():
    assert verify_house_style(draft("x" * 400, channel="sms"))
    # The same text as email is fine; the limit is a property of the channel.
    assert not verify_house_style(draft("x" * 400, channel="email"))


def test_a_clean_message_is_clean():
    assert not verify_house_style(
        draft(
            "Hi Sarah - one of our vans is off the road, so we need to move your fitting "
            "to today at 12:40. Sorry about that. Call us if that doesn't work."
        )
    )


# --------------------------------------------------------------- calibration


def test_a_judge_that_agrees_is_trusted():
    assert calibration(11, 12, {"good", "poor"}).trustworthy


def test_a_judge_that_disagrees_too_often_is_not():
    assert not calibration(6, 12, {"good", "poor"}).trustworthy


def test_a_judge_with_one_opinion_is_caught_however_well_it_scores():
    """The failure a percentage alone would let through.

    A judge answering "good" to everything gets half a balanced set right, which some
    thresholds would pass. It has no opinion, so its verdicts carry no information, and
    the tone scores built on it would be decoration.
    """
    degenerate = calibration(6, 12, {"good"})
    assert degenerate.agreement == 0.5
    assert not degenerate.discriminates
    assert not degenerate.trustworthy
    assert "no opinion" in degenerate.summary


def test_even_a_perfect_score_needs_two_opinions():
    """Contrived, but it pins the rule: agreement alone is never sufficient."""
    assert not calibration(12, 12, {"good"}).trustworthy


def test_the_threshold_is_a_competence_bar_not_a_quality_bar():
    """Guessing lands near 50% on a balanced set; reading the rubric should not."""
    assert 0.5 < MIN_AGREEMENT < 1.0


# ------------------------------------------------------------------ dataset


def test_the_labelled_set_is_balanced_and_unique():
    """An unbalanced set would let a judge biased toward the common answer look good."""
    labels = load_labels()
    assert len({label.id for label in labels}) == len(labels)
    good = sum(1 for label in labels if label.verdict == "good")
    assert good == len(labels) - good, "balance is what makes the agreement figure mean something"


def test_no_labelled_case_is_one_the_rules_already_catch():
    """Otherwise the judge scores well for work a regular expression did.

    This is the test that keeps the two halves honest: house style is checked, tone is
    judged, and a case that leaks from one into the other inflates the judge's
    agreement without it having read anything.
    """
    for label in load_labels():
        assert not verify_house_style(draft(label.body, channel="email")), (
            f"{label.id} is caught deterministically and does not belong in the judge's set"
        )
