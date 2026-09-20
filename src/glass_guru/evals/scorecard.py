"""Render an eval report as the comment that appears on a pull request.

A gate that only sets an exit code teaches people that a red X means "try again".
The useful artifact is the one that says *what* moved, by how much, and against what
it is being judged - in the diff, next to the change that caused it.

Deliberately reads the JSON report rather than an in-process `EvalReport`: the
offline tiers and the model tiers run as separate CI jobs on separate machines, so
the thing being rendered has always been through a file by the time anyone sees it.
The renderer is therefore also what proves the JSON report is complete enough to
reconstruct the verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from glass_guru.evals.core import MODEL_TIER_NUMBERS, REGRESSION_TOLERANCE

#: Metrics worth putting in front of a reviewer, and how to phrase them. Everything
#: else stays in the JSON for trending.
HEADLINE_METRICS: dict[str, tuple[str, str]] = {
    "repairs": ("mean repair attempts", "{:.2f}"),
    "escalated": ("escalation rate", "{:.1%}"),
    "violations": ("invariant violations", "{:.0f}"),
}


def _delta(current: float, before: float | None) -> str:
    """Movement in percentage points, with the regression verdict attached."""
    if before is None:
        return "new"
    change = current - before
    if abs(change) < 0.0005:
        return "no change"
    arrow = "up" if change > 0 else "down"
    flag = "  **regression**" if -change > REGRESSION_TOLERANCE else ""
    return f"{arrow} {abs(change):.1%}{flag}"


def render(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    """The pull-request comment, as GitHub-flavoured markdown."""
    previous = {t["label"]: t for t in (baseline or {}).get("tiers", [])}
    verdict = "passed" if report.get("passed") else "FAILED"

    lines = [
        f"### Eval scorecard - {verdict}",
        "",
        f"`{report.get('model_id', 'unknown')}` &middot; "
        f"{sum(int(t['cases']) for t in report['tiers'])} cases in "
        f"{float(report.get('duration_seconds', 0.0)):.0f}s",
        "",
        "| | tier | score | vs baseline | threshold | cases |",
        "|---|---|---|---|---|---|",
    ]

    for tier in report["tiers"]:
        before = previous.get(tier["label"])
        mark = "pass" if tier["passed"] else "**fail**"
        lines.append(
            f"| {mark} | {tier['tier']} {tier['label']} "
            f"| {float(tier['score']):.1%} "
            f"| {_delta(float(tier['score']), float(before['score']) if before else None)} "
            f"| {float(tier['threshold']):.0%} "
            f"| {tier['cases']} |"
        )

    health = _health(report)
    if health:
        lines += ["", f"**Model health** &middot; {health}"]

    failures = [
        (tier["label"], failure) for tier in report["tiers"] for failure in tier.get("failures", [])
    ]
    if failures:
        lines += [
            "",
            f"<details><summary>{len(failures)} failing case(s)</summary>",
            "",
        ]
        lines += [f"- `{label}` **{f['case_id']}** - {f['detail']}" for label, f in failures]
        lines += ["", "</details>"]

    for note in report.get("notes", []):
        lines += ["", f"> {note}"]

    return "\n".join(lines) + "\n"


def _health(report: dict[str, Any]) -> str:
    """Averages of the metrics a reviewer should actually look at."""
    collected: dict[str, list[float]] = {}
    for tier in report["tiers"]:
        for name, value in tier.get("metrics", {}).items():
            if name in HEADLINE_METRICS:
                collected.setdefault(name, []).append(float(value))
    parts = []
    for name, values in collected.items():
        label, fmt = HEADLINE_METRICS[name]
        parts.append(f"{label} {fmt.format(sum(values) / len(values))}")
    return " &middot; ".join(parts)


def merge(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fold several reports into one scorecard.

    The offline tiers and the model tiers run as separate jobs - the first needs no
    credentials and gates every pull request including forks, the second costs money
    and needs a role to assume. A reviewer should still see one table, so the comment
    is assembled from whichever reports actually ran.
    """
    tiers: dict[int, dict[str, Any]] = {}
    notes: list[str] = []
    models: list[str] = []
    for report in reports:
        for tier in report.get("tiers", []):
            # Last writer wins, but nothing writes the same tier twice: a tier belongs
            # to exactly one job by construction.
            tiers[int(tier["tier"])] = tier
        for note in report.get("notes", []):
            if note not in notes:
                notes.append(note)
        model = report.get("model_id")
        if model and model != "no model" and model not in models:
            models.append(model)

    # A job that never ran leaves no note behind, so the gap has to be noticed here.
    # A table that quietly lists two tiers instead of five reads as "all green", which
    # is exactly the wrong impression on a pull request nobody ran a model against.
    missing = sorted(MODEL_TIER_NUMBERS - set(tiers))
    if missing:
        notes.append(
            "Tiers " + ", ".join(str(t) for t in missing) + " did not run. "
            "The model tiers need a role to assume, which a fork cannot do."
        )

    return {
        "model_id": ", ".join(models) if models else "no model",
        "passed": all(r.get("passed") for r in reports),
        "duration_seconds": sum(float(r.get("duration_seconds", 0.0)) for r in reports),
        "tiers": [tiers[key] for key in sorted(tiers)],
        "notes": notes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glass-guru-scorecard", description=__doc__)
    parser.add_argument(
        "report", type=Path, nargs="+", help="JSON report(s) to render as one scorecard"
    )
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    args = parser.parse_args(argv)

    baseline = None
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text())

    found = [path for path in args.report if path.exists()]
    if not found:
        print("no eval reports to render", file=sys.stderr)
        return 1
    markdown = render(merge([json.loads(path.read_text()) for path in found]), baseline)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown)
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
