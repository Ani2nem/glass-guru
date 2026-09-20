"""Scoring framework.

Two rules shape everything here.

**Assert outcomes, not prose.** A scenario passes because the right jobs were served
and the right promises kept, never because the wording matched. Scoring text against
text produces a number that moves when a prompt is reworded and sits still when the
system gets worse, which is the opposite of what a gate is for.

**A failing case must say what to fix.** A score of 0.72 tells nobody anything. Every
result carries the specific fields, events or outcomes that disagreed, because the
report is read when something has broken and the reader is in a hurry.

Tiers exist so failures are legible by kind. An invariant breach is a defect; a drop
in extraction fidelity is a model or prompt problem; a scenario regression is a
behaviour change. Reporting them as one aggregate percentage would hide all three.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any


class Tier(IntEnum):
    """What kind of thing is being measured, and how hard the gate is."""

    #: Solver invariants. A single failure is a defect, not a statistic.
    INVARIANTS = 0
    #: Did the model read the input correctly - field by field, against gold labels.
    EXTRACTION = 1
    #: Did it then do the right thing: right action, right arguments.
    ACTION = 2
    #: End to end. Asserts what happened to the schedule, never how it was described.
    SCENARIO = 3
    #: Message quality. Grounding is checked deterministically; tone is judged.
    QUALITY = 4

    @property
    def label(self) -> str:
        return {
            Tier.INVARIANTS: "invariants",
            Tier.EXTRACTION: "extraction",
            Tier.ACTION: "action",
            Tier.SCENARIO: "scenario",
            Tier.QUALITY: "quality",
        }[self]


#: Tier numbers that need a live model. Named here rather than in the runner so the
#: scorecard can tell "scored badly" apart from "never ran" without importing it.
MODEL_TIER_NUMBERS = {1, 2, 4}

#: Ignore movement below this when comparing against a baseline. Sampling noise is not
#: a regression, and a gate that fires on it gets muted. Shared with the CI scorecard so
#: the comment on a pull request and the gate that blocks it cannot disagree about what
#: counts as a slip.
REGRESSION_TOLERANCE = 0.02

#: Pass marks per tier. Tier 0 is absolute: the point of an invariant is that it holds.
#: The others are deliberately below 1.0 - a suite that only passes at perfection stops
#: being a gate and becomes something people disable.
DEFAULT_THRESHOLDS: dict[Tier, float] = {
    Tier.INVARIANTS: 1.0,
    Tier.EXTRACTION: 0.85,
    Tier.ACTION: 0.90,
    Tier.SCENARIO: 0.95,
    Tier.QUALITY: 0.70,
}


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case, scored, with enough detail to act on a failure."""

    case_id: str
    tier: Tier
    score: float
    passed: bool
    detail: str = ""
    #: Per-case numbers worth trending: repair attempts, escalations, latency.
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def icon(self) -> str:
        return "ok " if self.passed else "FAIL"


@dataclass(frozen=True, slots=True)
class TierResult:
    tier: Tier
    results: tuple[CaseResult, ...]
    threshold: float

    @property
    def score(self) -> float:
        """Mean case score. Empty tiers score 1.0 - nothing measured is not a failure,
        though `cases` makes the emptiness visible in the report."""
        return sum(r.score for r in self.results) / len(self.results) if self.results else 1.0

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def cases(self) -> int:
        return len(self.results)

    def metric(self, name: str) -> float:
        """Mean of a named per-case metric across this tier."""
        values = [r.metrics[name] for r in self.results if name in r.metrics]
        return sum(values) / len(values) if values else 0.0

    @property
    def metric_names(self) -> tuple[str, ...]:
        """Every per-case metric recorded anywhere in this tier."""
        names: set[str] = set()
        for result in self.results:
            names.update(result.metrics)
        return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The whole run: what was measured, against which model, and whether it passes."""

    model_id: str
    tiers: tuple[TierResult, ...]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.tiers)

    @property
    def total_cases(self) -> int:
        return sum(t.cases for t in self.tiers)

    def tier(self, tier: Tier) -> TierResult | None:
        return next((t for t in self.tiers if t.tier is tier), None)

    # ------------------------------------------------------------------ output

    def render(self, verbose: bool = False) -> str:
        lines = [
            f"EVAL  {self.model_id}",
            f"      {self.total_cases} case(s) in {self.duration_seconds:.1f}s",
            "-" * 78,
            "",
        ]
        for tier in self.tiers:
            mark = "PASS" if tier.passed else "FAIL"
            lines.append(
                f"  {mark}  tier {int(tier.tier)} {tier.tier.label:<12} "
                f"{tier.score:>6.1%}  (need {tier.threshold:.0%})  "
                f"{tier.cases} case(s)"
            )
            for failure in tier.failures:
                lines.append(f"          {failure.case_id}: {failure.detail}")
            if verbose:
                for result in tier.results:
                    if result.passed:
                        lines.append(f"          {result.icon} {result.case_id} {result.score:.0%}")

        # Health signals for a small model. A rising repair rate means it is drifting
        # from what the prompt and schema expect, long before a schedule looks wrong.
        repairs = [t.metric("repairs") for t in self.tiers if t.metric("repairs")]
        escalations = [t.metric("escalated") for t in self.tiers if t.cases]
        if repairs or escalations:
            lines += ["", "  model health"]
            if repairs:
                lines.append(f"      mean repair attempts   {sum(repairs) / len(repairs):.2f}")
            if escalations:
                lines.append(
                    f"      escalation rate        {sum(escalations) / len(escalations):.1%}"
                )

        for note in self.notes:
            lines += ["", f"  note: {note}"]
        lines += ["", "-" * 78, f"  {'PASSED' if self.passed else 'FAILED'}"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable, for CI comparison against a baseline."""
        return {
            "model_id": self.model_id,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "passed": self.passed,
            "tiers": [
                {
                    "tier": int(t.tier),
                    "label": t.tier.label,
                    "score": round(t.score, 4),
                    "threshold": t.threshold,
                    "passed": t.passed,
                    "cases": t.cases,
                    "failures": [{"case_id": f.case_id, "detail": f.detail} for f in t.failures],
                    # Health signals belong in the machine report, not only the
                    # printed one. A CI scorecard cannot trend a number it never sees,
                    # and repair rate is the earliest warning that a small model is
                    # drifting from what the schema expects.
                    "metrics": {name: round(t.metric(name), 4) for name in t.metric_names},
                }
                for t in self.tiers
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def compare(baseline: dict[str, Any], current: EvalReport) -> tuple[bool, list[str]]:
    """Has anything got worse since the baseline?

    Separate from the thresholds on purpose. A tier can sit comfortably above its gate
    and still be sliding; catching that early is the difference between noticing a
    regression and discovering it three months later as a mystery.
    """
    problems: list[str] = []
    previous = {t["label"]: t for t in baseline.get("tiers", [])}

    for tier in current.tiers:
        before = previous.get(tier.tier.label)
        if before is None:
            continue
        drop = float(before["score"]) - tier.score
        if drop > REGRESSION_TOLERANCE:
            problems.append(
                f"tier {int(tier.tier)} {tier.tier.label}: "
                f"{float(before['score']):.1%} -> {tier.score:.1%} ({drop:.1%} worse)"
            )
    return not problems, problems


def build_report(
    model_id: str,
    results: Sequence[CaseResult],
    *,
    thresholds: dict[Tier, float] | None = None,
    duration_seconds: float = 0.0,
    notes: Sequence[str] = (),
) -> EvalReport:
    gates = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    by_tier: dict[Tier, list[CaseResult]] = {}
    for result in results:
        by_tier.setdefault(result.tier, []).append(result)

    return EvalReport(
        model_id=model_id,
        tiers=tuple(
            TierResult(tier=tier, results=tuple(by_tier[tier]), threshold=gates[tier])
            for tier in sorted(by_tier)
        ),
        duration_seconds=duration_seconds,
        notes=tuple(notes),
    )
