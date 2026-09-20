"""The pull-request comment is a deliverable, so it gets tested like one.

The thing worth protecting here is agreement: the comment must call a slip a
regression exactly when the gate would block on it. Two independent copies of that
threshold is how a scorecard ends up saying "looks fine" on a blocked merge.
"""

from __future__ import annotations

from typing import Any

from glass_guru.evals.core import REGRESSION_TOLERANCE
from glass_guru.evals.scorecard import merge, render


def report(
    score: float, *, tier: int = 1, label: str = "extraction", **over: Any
) -> dict[str, Any]:
    return {
        "model_id": "test-model",
        "passed": over.pop("passed", True),
        "duration_seconds": 12.0,
        "notes": over.pop("notes", []),
        "tiers": [
            {
                "tier": tier,
                "label": label,
                "score": score,
                "threshold": 0.85,
                "passed": score >= 0.85,
                "cases": 22,
                "failures": over.pop("failures", []),
                "metrics": over.pop("metrics", {}),
            }
        ],
    }


def test_a_drop_past_the_gate_is_called_a_regression():
    """The word must appear exactly when `compare` would fail the build."""
    before = report(0.95)
    after = report(0.95 - REGRESSION_TOLERANCE - 0.01)
    assert "**regression**" in render(after, before)


def test_a_drop_inside_the_tolerance_is_not():
    before = report(0.95)
    after = report(0.95 - REGRESSION_TOLERANCE + 0.005)
    markdown = render(after, before)
    assert "**regression**" not in markdown
    assert "down" in markdown, "a real move should still be shown, just not flagged"


def test_an_improvement_is_never_flagged():
    assert "**regression**" not in render(report(0.99), report(0.85))


def test_a_tier_with_no_baseline_reads_as_new():
    assert "new" in render(report(0.9), {"tiers": []})


def test_failing_cases_are_listed_for_the_reviewer():
    markdown = render(
        report(0.5, passed=False, failures=[{"case_id": "triage-van-down", "detail": "asked"}])
    )
    assert "FAILED" in markdown
    assert "triage-van-down" in markdown
    assert "asked" in markdown


def test_merging_two_jobs_gives_one_table():
    """Offline and model tiers run on separate machines; the reviewer sees one table."""
    offline = report(1.0, tier=0, label="invariants", metrics={"violations": 0.0})
    model = report(0.95, tier=1, label="extraction", metrics={"repairs": 0.25})
    merged = merge([model, offline])

    assert [t["tier"] for t in merged["tiers"]] == [0, 1], "tiers sort regardless of job order"
    assert merged["duration_seconds"] == 24.0
    markdown = render(merged)
    assert "invariant violations 0" in markdown
    assert "mean repair attempts 0.25" in markdown


def test_one_failing_job_fails_the_whole_scorecard():
    merged = merge([report(1.0, tier=0, label="invariants"), report(0.1, passed=False)])
    assert merged["passed"] is False
    assert "FAILED" in render(merged)


def test_a_skipped_model_job_does_not_claim_a_model_ran():
    """A fork pull request runs the offline tiers only. The comment must say so."""
    offline = {**report(1.0, tier=0, label="invariants"), "model_id": "no model"}
    merged = merge([offline])
    assert merged["model_id"] == "no model"
    assert "`no model`" in render(merged)


def test_unrun_model_tiers_are_called_out():
    """Two green rows where five were expected must not read as all green."""
    merged = merge([report(1.0, tier=0, label="invariants")])
    assert "Tiers 1, 2, 4 did not run" in render(merged)


def test_a_full_run_says_nothing_about_missing_tiers():
    merged = merge(
        [
            report(1.0, tier=0, label="invariants"),
            report(0.95, tier=1, label="extraction"),
            report(1.0, tier=2, label="action"),
            report(1.0, tier=4, label="quality"),
        ]
    )
    assert "did not run" not in render(merged)
