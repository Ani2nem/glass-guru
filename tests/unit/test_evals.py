"""The eval suite, evaluated.

A scoring harness that cannot distinguish a good model from a bad one is worse than
none: it produces a number people trust and a gate that never fires. So the central
tests here feed the runners a provider that answers correctly and one that answers
badly, and assert the scores separate.

The rest pin the scoring rules that make the number mean something - weighting,
skipping unstated fields, and refusing to average a fabrication into a pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from glass_guru.agents.llm.base import LLMRequest, LLMResponse
from glass_guru.evals.core import (
    CaseResult,
    Tier,
    build_report,
    compare,
)
from glass_guru.evals.runners import _load, run_extraction, run_invariants, run_scenarios
from glass_guru.evals.scoring import (
    CALL_FIELDS,
    FieldSpec,
    Match,
    score_field,
    score_fields,
)

# ----------------------------------------------------------------- field scoring


def test_a_phone_number_written_differently_is_the_same_number():
    spec = FieldSpec("phone", Match.DIGITS)
    assert score_field(spec, "2065550142", "206-555-0142").correct
    assert score_field(spec, "2065550142", "(206) 555 0142").correct
    assert not score_field(spec, "2065550142", "2065550143").correct


def test_set_fields_score_partial_credit():
    spec = FieldSpec("signals", Match.SET)
    assert score_field(spec, ["a", "b"], ["a", "b"]).score == 1.0
    assert 0 < score_field(spec, ["a", "b"], ["a"]).score < 1.0
    assert score_field(spec, ["a"], ["z"]).score == 0.0


def test_both_empty_sets_is_a_correct_answer():
    """ "Nothing was said" is a real answer, not a division by zero."""
    assert score_field(FieldSpec("signals", Match.SET), [], []).correct


def test_presence_fields_ignore_wording():
    """Site notes phrased differently are not a regression; dropping them is."""
    spec = FieldSpec("site_notes", Match.PRESENCE)
    assert score_field(spec, "gate code 4412", "code 4412, use the alley").correct
    assert not score_field(spec, "gate code 4412", "").correct


def test_unstated_fields_are_skipped_not_scored():
    """Counting a blank gold label as a pass inflates every score by however many
    details a call happened not to mention, making an easy case look like a good model."""
    gold: dict[str, Any] = {"customer_name": "Sarah", "phone": ""}
    scored = score_fields(gold, {"customer_name": "Sarah", "phone": "555"}, CALL_FIELDS)
    assert [o.name for o in scored.outcomes] == ["customer_name"]
    assert scored.score == 1.0


def test_weighting_makes_important_fields_count_more():
    """A wrong callback number must hurt more than a wrong property type."""
    gold = {"phone": "2065550142", "property_type": "residential", "service_type": "auto_glass"}
    wrong_phone = score_fields(gold, {**gold, "phone": "9999999999"}, CALL_FIELDS).score
    wrong_type = score_fields(gold, {**gold, "property_type": "commercial"}, CALL_FIELDS).score
    assert wrong_phone < wrong_type


def test_a_failing_score_names_the_field():
    """ "0.72" tells nobody anything. The report is read when something has broken."""
    gold = {"phone": "2065550142", "service_type": "auto_glass"}
    scored = score_fields(gold, {"phone": "999", "service_type": "auto_glass"}, CALL_FIELDS)
    assert "phone" in scored.summary()


# ------------------------------------------------------------------- reporting


def result(tier: Tier, score: float, passed: bool = True) -> CaseResult:
    return CaseResult(case_id=f"c{score}", tier=tier, score=score, passed=passed)


def test_tier_zero_admits_no_failures():
    """The point of an invariant is that it holds. A 99% pass rate is a defect."""
    report = build_report("m", [result(Tier.INVARIANTS, 1.0), result(Tier.INVARIANTS, 0.0)])
    assert not report.passed


def test_other_tiers_have_room_below_perfect():
    """A suite that only passes at perfection stops being a gate and gets disabled."""
    report = build_report("m", [result(Tier.EXTRACTION, 0.9), result(Tier.EXTRACTION, 0.95)])
    assert report.passed


def test_a_report_round_trips_through_json():
    report = build_report("m", [result(Tier.SCENARIO, 1.0)])
    import json

    restored = json.loads(report.to_json())
    assert restored["passed"] is True
    assert restored["tiers"][0]["label"] == "scenario"


def test_the_render_names_failing_cases():
    failing = CaseResult(
        case_id="intake-plain",
        tier=Tier.EXTRACTION,
        score=0.2,
        passed=False,
        detail="phone: expected '2065550142', got ''",
    )
    text = build_report("m", [failing]).render()
    assert "intake-plain" in text
    assert "2065550142" in text


# ------------------------------------------------------------ baseline comparison


def baseline_of(score: float) -> dict[str, Any]:
    return {"tiers": [{"label": "extraction", "score": score, "threshold": 0.85}]}


def test_a_slide_is_caught_even_while_still_passing():
    """How quality actually degrades: not in one visible step but a series of small
    ones nobody objected to."""
    current = build_report("m", [result(Tier.EXTRACTION, 0.88)])
    ok, problems = compare(baseline_of(0.97), current)
    assert not ok
    assert "extraction" in problems[0]


def test_noise_is_not_a_regression():
    """A gate that fires on sampling noise gets muted, and then it is not a gate."""
    current = build_report("m", [result(Tier.EXTRACTION, 0.96)])
    ok, _ = compare(baseline_of(0.97), current)
    assert ok


def test_improvement_is_never_a_regression():
    current = build_report("m", [result(Tier.EXTRACTION, 1.0)])
    assert compare(baseline_of(0.85), current)[0]


# ----------------------------------------------------- the harness discriminates


class GoldProvider:
    """Answers every case with its own gold label, by matching on the input text."""

    model_id = "gold"

    def __init__(self) -> None:
        self._intake = {c["text"].strip(): c["expect"] for c in _load("intake")["cases"]}
        self._triage = {c["text"].strip(): c["expect"] for c in _load("triage")["cases"]}

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = request.messages[-1].text.strip()
        if (expect := self._intake.get(text)) is not None:
            return LLMResponse(structured=_as_call(expect), stop_reason="tool_use")
        if (expect := self._triage.get(text)) is not None:
            return LLMResponse(structured=_as_triage(expect), stop_reason="tool_use")
        return LLMResponse(structured={}, stop_reason="tool_use")


class UselessProvider:
    """Answers everything the same way: the shape is valid, the content is wrong."""

    model_id = "useless"

    def complete(self, request: LLMRequest) -> LLMResponse:
        if "dispatcher" in request.system or "typed events" in request.system:
            return LLMResponse(
                structured={"events": [], "question": "", "summary": ""},
                stop_reason="tool_use",
            )
        return LLMResponse(
            structured={
                "customer_name": "Someone",
                "phone": "0000000000",
                "address": "",
                "service_type": "measure_quote",
                "urgency": "low",
                "commitment_signals": [],
            },
            stop_reason="tool_use",
        )


def _as_call(expect: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_name": expect.get("customer_name", ""),
        "phone": expect.get("phone", ""),
        "address": expect.get("address", ""),
        "service_type": expect.get("service_type", ""),
        "pane_count": expect.get("pane_count"),
        "property_type": expect.get("property_type", ""),
        "urgency": expect.get("urgency", "normal"),
        "hard_constraint": expect.get("hard_constraint", ""),
        "glass_type": expect.get("glass_type", ""),
        "commitment_signals": expect.get("commitment_signals", []),
        "commitment_quotes": [],
        "site_notes": expect.get("site_notes", ""),
    }


def _as_triage(expect: dict[str, Any]) -> dict[str, Any]:
    kinds = expect.get("event_kinds", [])
    targets = expect.get("targets", [])
    events = []
    for index, kind in enumerate(kinds):
        event: dict[str, Any] = {"kind": kind}
        if index < len(targets):
            event["target"] = targets[index]
        if kind == "job-overran":
            event["minutes"] = 120
        if kind == "job-completed":
            event["minutes"] = 90
        if kind == "traffic-delay":
            event["multiplier"] = 2.0
        events.append(event)
    return {
        "events": events,
        "question": "which one?" if expect.get("asks_question") else "",
        "summary": "",
    }


@pytest.fixture
def frozen_travel(monkeypatch):
    monkeypatch.setenv("GLASS_GURU_TRAVEL", "frozen")


def test_a_correct_model_scores_near_the_top(frozen_travel):
    results = run_extraction(GoldProvider())
    score = sum(r.score for r in results) / len(results)
    assert score > 0.9, [(r.case_id, round(r.score, 2), r.detail) for r in results if r.score < 0.9]


def test_a_useless_model_scores_badly(frozen_travel):
    """The number has to move. A harness that scores everything highly is a harness
    that will never block anything."""
    results = run_extraction(UselessProvider())
    score = sum(r.score for r in results) / len(results)
    assert score < 0.6


def test_the_two_are_clearly_separated(frozen_travel):
    good = run_extraction(GoldProvider())
    bad = run_extraction(UselessProvider())
    gap = (sum(r.score for r in good) / len(good)) - (sum(r.score for r in bad) / len(bad))
    assert gap > 0.35, f"only {gap:.2f} between a correct and a useless model"


# ------------------------------------------------------------ model-free tiers


def test_invariants_run_without_a_model(frozen_travel):
    results = run_invariants()
    assert results
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]


def test_scenarios_assert_outcomes_and_pass(frozen_travel):
    results = run_scenarios()
    assert results
    assert all(r.passed for r in results), [(r.case_id, r.detail) for r in results if not r.passed]


def test_every_scenario_in_the_library_has_an_envelope():
    """A scenario nobody asserted anything about is a scenario that cannot fail."""
    from glass_guru.fixtures import scenarios as library

    covered = {c["scenario"] for c in _load("scenarios")["cases"]}
    assert covered == set(library.SCENARIOS)


def test_the_datasets_say_they_are_synthetic():
    """The caveat has to travel with the data. A score that looks like evidence and
    is not is worse than no score."""
    for name in ("intake", "triage"):
        assert "synthetic" in _load(name)["meta"]["note"].lower()


# ------------------------------------------------- the gate catches a weak checker


def test_the_checker_still_catches_every_mutation(frozen_travel):
    from glass_guru.evals.runners import run_checker_detection

    results = run_checker_detection()
    assert results
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]


def test_removing_a_check_fails_the_gate(frozen_travel, monkeypatch):
    """The reason detection cases exist.

    Tier 0 originally only solved and validated, which proves the *solver* agrees with
    the checker. It says nothing about the checker: removing a check produces a
    *missing* violation rather than a detected one, so a checker returning no
    violations at all scored a hundred percent. Deleting the certification check and
    watching the gate pass is what surfaced this.
    """
    from glass_guru.domain import invariants
    from glass_guru.evals.runners import run_checker_detection

    original = invariants._check_crew_fitness
    monkeypatch.setattr(invariants, "_check_crew_fitness", lambda plan, world: [])

    results = run_checker_detection()
    failed = [r for r in results if not r.passed]
    assert failed, "a checker missing its certification check passed the gate"
    assert any("missing_certification" in r.detail for r in failed)

    monkeypatch.setattr(invariants, "_check_crew_fitness", original)


def test_every_mutation_names_a_distinct_defect():
    """A mutation whose expected code another already covers adds nothing."""
    from glass_guru.evals.runners import MUTATIONS

    assert len({m.name for m in MUTATIONS}) == len(MUTATIONS)
    assert all(m.why for m in MUTATIONS)


def test_mutations_do_not_leak_state_between_cases(frozen_travel):
    """Some mutations alter the world. A leaked change would silently weaken the next
    case, and a weakened case is one that cannot fail."""
    from glass_guru.evals.runners import run_checker_detection

    first = run_checker_detection()
    second = run_checker_detection()
    assert [r.passed for r in first] == [r.passed for r in second]
