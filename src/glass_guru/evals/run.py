"""The eval runner.

Two jobs. Locally it answers "is this better or worse than before". In CI it is a gate:
a non-zero exit blocks a merge, either because a tier fell below its threshold or
because it slipped against the committed baseline.

Both matter. Thresholds catch something being broken; baseline comparison catches
something sliding while still technically passing, which is how quality usually
degrades - not in one visible step but in a series of small ones nobody objected to.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.llm.factory import LLMMode, build_llm, describe
from glass_guru.evals.core import CaseResult, EvalReport, Tier, build_report, compare
from glass_guru.evals.runners import (
    run_actions,
    run_checker_detection,
    run_extraction,
    run_invariants,
    run_quality,
    run_scenarios,
)
from glass_guru.obs.tracing import configure

BASELINE = Path(__file__).resolve().parents[3] / "evals" / "baseline.json"

#: Tiers that need a model. The rest run anywhere, including in a fresh clone.
MODEL_TIERS = {Tier.EXTRACTION, Tier.ACTION, Tier.QUALITY}


def run(
    provider: LLMProvider | None,
    tiers: Sequence[Tier],
) -> EvalReport:
    started = time.monotonic()
    results: list[CaseResult] = []
    notes: list[str] = [
        "Datasets are synthetic and were written alongside the prompts they test. "
        "A passing score means unchanged, not correct on real calls.",
    ]

    if Tier.INVARIANTS in tiers:
        # Both halves: the solver agrees with the checker, and the checker still
        # objects when it should.
        results += run_invariants()
        results += run_checker_detection()
    if Tier.SCENARIO in tiers:
        results += run_scenarios()

    wanted_model_tiers = [t for t in tiers if t in MODEL_TIERS]
    if wanted_model_tiers:
        if provider is None:
            # Skipped rather than failed. Reporting failure when nobody was there to
            # answer teaches people to ignore the report.
            notes.append(
                "Tiers "
                + ", ".join(str(int(t)) for t in wanted_model_tiers)
                + " skipped: no model provider configured. See docs/aws-setup.md."
            )
        else:
            if Tier.EXTRACTION in tiers:
                results += run_extraction(provider)
            if Tier.ACTION in tiers:
                results += run_actions(provider)
            if Tier.QUALITY in tiers:
                results += run_quality(provider)

    return build_report(
        model_id=describe(provider) if provider else "no model",
        results=results,
        duration_seconds=time.monotonic() - started,
        notes=notes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glass-guru-eval", description="Run the eval suite.")
    parser.add_argument(
        "--tier",
        action="append",
        type=int,
        choices=[0, 1, 2, 3, 4],
        help="run only these tiers; repeatable. Default: all.",
    )
    parser.add_argument("--llm", default=None, choices=[m.value for m in LLMMode])
    parser.add_argument("--model-id", default=None, help="override the model id")
    parser.add_argument("--json", type=Path, default=None, help="write a machine report")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="compare against a previous report and fail on a regression",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="overwrite the committed baseline with this run",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure(service="glass-guru-eval")
    tiers = [Tier(t) for t in (args.tier or [0, 1, 2, 3, 4])]

    provider: LLMProvider | None = None
    if any(t in MODEL_TIERS for t in tiers):
        candidate = build_llm(args.llm, model_id=args.model_id)
        # An unavailable provider is not a provider. Running the model tiers against
        # it would score every case zero and report a catastrophe that never happened.
        provider = None if candidate.model_id == "unavailable" else candidate

    report = run(provider, tiers)
    print(report.render(verbose=args.verbose))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(report.to_json() + "\n")
        print(f"\nwrote {args.json}")

    if args.update_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(report.to_json() + "\n")
        print(f"baseline updated: {BASELINE}")
        return 0

    exit_code = 0 if report.passed else 1

    baseline_path = args.baseline or (BASELINE if BASELINE.exists() else None)
    if baseline_path and baseline_path.exists():
        ok, problems = compare(json.loads(baseline_path.read_text()), report)
        if not ok:
            print("\nREGRESSION against the baseline:")
            for problem in problems:
                print(f"  {problem}")
            exit_code = 1
        else:
            print("\nno regression against the baseline")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
