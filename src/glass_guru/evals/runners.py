"""Running each tier.

Tiers 0 and 3 need no model and always run. Tiers 1, 2 and 4 need one, and are skipped
with a visible note when none is configured rather than silently scoring zero - a
suite that reports failure when nobody was there to answer teaches people to ignore it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from glass_guru.agents.comms import draft_customer_messages
from glass_guru.agents.coordinator import choose_repair
from glass_guru.agents.intake import intake
from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.triage import triage
from glass_guru.config import BusinessParams
from glass_guru.domain.autonomy import AutonomyPolicy
from glass_guru.domain.enums import ViolationCode
from glass_guru.domain.invariants import ValidationConfig, summarize, validate_plan
from glass_guru.domain.models import PlanVersion
from glass_guru.domain.state import WorldState, fold
from glass_guru.evals.core import CaseResult, Tier
from glass_guru.evals.scoring import CALL_FIELDS, TRIAGE_FIELDS, score_fields
from glass_guru.fixtures import scenarios as scenario_library
from glass_guru.fixtures.sample_business import WEEK_START, _at, sample_world_at
from glass_guru.obs.correlation import dispatch
from glass_guru.scheduler.day_planner import SolveParams, plan_day
from glass_guru.scheduler.horizon import HorizonParams, plan_horizon
from glass_guru.scheduler.repair import repair_plan
from glass_guru.scheduler.travel.factory import TravelMode, build_travel

DATASETS = Path(__file__).resolve().parent / "datasets"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DATASETS / f"{name}.yaml").read_text())
    return data


def _business() -> BusinessParams:
    return BusinessParams.load()


def _tz() -> ZoneInfo:
    return ZoneInfo(_business().meta.timezone)


# ------------------------------------------------------------------ tier 0


def run_invariants() -> list[CaseResult]:
    """Every scenario, solved and checked. Zero tolerance.

    Property-based coverage lives in ``tests/property`` and runs under pytest; this
    tier replays the named scenarios so a CI report shows them by name.
    """
    business = _business()
    travel = build_travel(business, TravelMode.FROZEN)
    params = SolveParams.from_business(business, _tz())
    results: list[CaseResult] = []

    for name, scenario in sorted(scenario_library.SCENARIOS.items()):
        world = scenario.world()
        day = plan_day(
            world=world,
            travel=travel,
            on_date=scenario.solve_date,
            candidate_job_ids=[j.id for j in world.active_jobs()],
            params=params,
        )
        plan = PlanVersion(
            id=name,
            created_at=world.as_of,
            horizon_start=scenario.solve_date,
            horizon_end=scenario.solve_date,
            routes=day.routes,
        )
        violations = validate_plan(plan, world, travel, ValidationConfig(business_tz=_tz()))
        results.append(
            CaseResult(
                case_id=f"invariants/{name}",
                tier=Tier.INVARIANTS,
                score=0.0 if violations else 1.0,
                passed=not violations,
                detail=summarize(violations) if violations else "",
                metrics={"violations": float(len(violations))},
            )
        )
    return results


# ------------------------------------------------------------------ tier 1


def run_extraction(provider: LLMProvider) -> list[CaseResult]:
    """Did the model read the input correctly, field by field."""
    business = _business()
    results: list[CaseResult] = []

    for case in _load("intake")["cases"]:
        with dispatch(f"eval-{case['id']}"):
            outcome = intake(provider, case["text"], business=business, now=_at(0, 9))
        call = outcome.call
        actual = call.model_dump(mode="json") if call else {}
        score = score_fields(case["expect"], actual, CALL_FIELDS)
        results.append(
            CaseResult(
                case_id=case["id"],
                tier=Tier.EXTRACTION,
                score=score.score,
                passed=score.score >= 0.85,
                detail=score.summary(),
                metrics={
                    "repairs": float(outcome.extraction.repairs),
                    "escalated": 1.0 if outcome.extraction.escalated else 0.0,
                },
            )
        )

    events, now = sample_world_at()
    world = fold(events, as_of=now)
    for case in _load("triage")["cases"]:
        with dispatch(f"eval-{case['id']}"):
            outcome = triage(  # type: ignore[assignment]
                provider, world, case["text"], on_date=WEEK_START, tz=_tz()
            )
        actual = _triage_actual(outcome)
        score = score_fields(case["expect"], actual, TRIAGE_FIELDS)
        results.append(
            CaseResult(
                case_id=case["id"],
                tier=Tier.EXTRACTION,
                score=score.score,
                passed=score.score >= 0.85,
                detail=score.summary(),
                metrics={
                    "repairs": float(outcome.extraction.repairs),
                    "escalated": 1.0 if outcome.extraction.escalated else 0.0,
                },
            )
        )
    return results


def _triage_actual(outcome: Any) -> dict[str, Any]:
    kinds = {str(event.type).replace("_", "-") for event in outcome.events}
    targets = {
        str(
            getattr(event, "van_id", "")
            or getattr(event, "worker_id", "")
            or getattr(event, "job_id", "")
        )
        for event in outcome.events
    }
    return {
        "event_kinds": sorted(kinds),
        "targets": sorted(t for t in targets if t),
        "asks_question": bool(outcome.question),
    }


# ------------------------------------------------------------------ tier 2


def run_actions(provider: LLMProvider) -> list[CaseResult]:
    """Having read it, did the model do the right thing?

    Reframed from the original "tool-call correctness". The agents here reach the
    engine through one forced structured call rather than an open tool loop, so the
    equivalent property is whether the *action* is right: the correct event kinds
    against the correct targets, and a strategy the engine actually offered. Same
    question - right action, right arguments - asked of what was actually built.
    """
    events, now = sample_world_at()
    world = fold(events, as_of=now)
    results: list[CaseResult] = []

    for case in _load("triage")["cases"]:
        expect = case["expect"]
        with dispatch(f"eval-act-{case['id']}"):
            outcome = triage(provider, world, case["text"], on_date=WEEK_START, tz=_tz())
        actual = _triage_actual(outcome)

        kinds_right = set(actual["event_kinds"]) == set(expect["event_kinds"])
        targets_right = set(actual["targets"]) == set(expect["targets"])
        # A hallucinated id is worse than a missed one: it is an action against
        # something that does not exist.
        no_invention = not outcome.unknown_targets
        score = (kinds_right + targets_right + no_invention) / 3.0

        problems: list[str] = []
        if not kinds_right:
            problems.append(f"kinds {actual['event_kinds']} != {expect['event_kinds']}")
        if not targets_right:
            problems.append(f"targets {actual['targets']} != {expect['targets']}")
        if not no_invention:
            problems.append(f"invented ids {list(outcome.unknown_targets)}")

        results.append(
            CaseResult(
                case_id=f"action/{case['id']}",
                tier=Tier.ACTION,
                score=score,
                passed=score >= 0.999,
                detail="; ".join(problems),
                metrics={"repairs": float(outcome.extraction.repairs)},
            )
        )

    results.append(_coordinator_case(provider))
    return results


def _coordinator_case(provider: LLMProvider) -> CaseResult:
    """The coordinator must pick a strategy the engine actually offered."""
    business = _business()
    travel = build_travel(business, TravelMode.FROZEN)
    params = SolveParams.from_business(business, _tz())
    horizon_params = HorizonParams.from_business(business)

    scenario = scenario_library.get("van_breakdown")
    world = scenario.world()
    base = plan_horizon(
        world=world,
        travel=travel,
        start=WEEK_START,
        params=params,
        horizon_params=horizon_params,
    )
    baseline = PlanVersion(
        id="eval-baseline",
        created_at=world.as_of,
        horizon_start=WEEK_START,
        horizon_end=date.fromordinal(WEEK_START.toordinal() + horizon_params.days - 1),
        routes=base.routes,
    )
    options = repair_plan(
        world=world,
        travel=travel,
        baseline=baseline,
        start=WEEK_START,
        params=params,
        horizon_params=horizon_params,
    )
    offered = {c.strategy.name for c in options.candidates}

    with dispatch("eval-coordinator"):
        decision = choose_repair(provider, options, AutonomyPolicy.from_business(business))

    chosen = decision.candidate.strategy.name if decision.candidate else ""
    valid = chosen in offered
    explained = bool(decision.rationale)
    escalated = decision.extraction.escalated

    return CaseResult(
        case_id="action/coordinator-picks-an-offered-strategy",
        tier=Tier.ACTION,
        score=1.0 if (valid and explained and not escalated) else 0.0,
        passed=valid and explained and not escalated,
        detail=(
            f"chose {chosen!r}, offered {sorted(offered)}"
            if not valid
            else ("no rationale given" if not explained else "")
        ),
        metrics={
            "repairs": float(decision.extraction.repairs),
            "escalated": 1.0 if escalated else 0.0,
        },
    )


# ------------------------------------------------------------------ tier 3


def run_scenarios() -> list[CaseResult]:
    """End to end, asserting outcomes rather than prose."""
    business = _business()
    travel = build_travel(business, TravelMode.FROZEN)
    params = SolveParams.from_business(business, _tz())
    results: list[CaseResult] = []

    for case in _load("scenarios")["cases"]:
        scenario = scenario_library.get(case["scenario"])
        world = scenario.world()
        day = plan_day(
            world=world,
            travel=travel,
            on_date=scenario.solve_date,
            candidate_job_ids=[j.id for j in world.active_jobs()],
            params=params,
        )
        plan = PlanVersion(
            id=case["scenario"],
            created_at=world.as_of,
            horizon_start=scenario.solve_date,
            horizon_end=scenario.solve_date,
            routes=day.routes,
        )
        violations = validate_plan(plan, world, travel, ValidationConfig(business_tz=_tz()))
        results.append(_check_envelope(case, world, day, violations))
    return results


def _check_envelope(
    case: dict[str, Any], world: WorldState, day: Any, violations: tuple[Any, ...]
) -> CaseResult:
    expect = case["expect"]
    served = {job_id for route in day.routes for job_id in route.job_ids}
    reasons = {u.job_id: u.reason.value for u in day.unserved}
    problems: list[str] = []

    if expect.get("feasible") and violations:
        problems.append(f"{len(violations)} invariant violation(s)")
    if len(served) < expect.get("min_jobs_served", 0):
        problems.append(f"served {len(served)}, expected at least {expect['min_jobs_served']}")
    for job_id in expect.get("must_serve", []):
        if job_id not in served:
            problems.append(f"{job_id} was not served")
    for job_id in expect.get("must_not_serve", []):
        if job_id in served:
            problems.append(f"{job_id} was served but should not have been")
    for job_id, reason in (expect.get("unserved_reasons") or {}).items():
        if reasons.get(job_id) != reason:
            problems.append(f"{job_id} reported {reasons.get(job_id)!r}, expected {reason!r}")

    return CaseResult(
        case_id=f"scenario/{case['scenario']}",
        tier=Tier.SCENARIO,
        score=0.0 if problems else 1.0,
        passed=not problems,
        detail="; ".join(problems),
        metrics={"served": float(len(served))},
    )


# ------------------------------------------------------------------ tier 4


def run_quality(provider: LLMProvider) -> list[CaseResult]:
    """Customer messages: grounded first, then readable.

    Grounding is checked deterministically and is not negotiable - a message stating a
    time the plan does not support fails outright, however well written. Tone is the
    part worth a judge, and is scored separately so a well-phrased fabrication cannot
    average its way to a pass.
    """
    business = _business()
    travel = build_travel(business, TravelMode.FROZEN)
    params = SolveParams.from_business(business, _tz())
    horizon_params = HorizonParams.from_business(business)
    results: list[CaseResult] = []

    for name in ("van_breakdown", "commercial_crew_out"):
        scenario = scenario_library.get(name)
        world = scenario.world()
        base = plan_horizon(
            world=world,
            travel=travel,
            start=WEEK_START,
            params=params,
            horizon_params=horizon_params,
        )
        baseline = PlanVersion(
            id=f"eval-{name}",
            created_at=world.as_of,
            horizon_start=WEEK_START,
            horizon_end=date.fromordinal(WEEK_START.toordinal() + horizon_params.days - 1),
            routes=base.routes,
        )
        options = repair_plan(
            world=world,
            travel=travel,
            baseline=baseline,
            start=WEEK_START,
            params=params,
            horizon_params=horizon_params,
        )
        candidate = options.best_by_fewest_calls
        if candidate is None or not candidate.diff.customer_visible_changes:
            # Nobody needed telling. Drafting anything here would itself be a failure,
            # and there is a unit test for that; nothing to score.
            continue

        with dispatch(f"eval-comms-{name}"):
            drafted = draft_customer_messages(
                provider, candidate.diff, tz=_tz(), reason="a van broke down"
            )

        issues = [f"{i.phrase}: {i.detail}" for i in drafted.issues]
        results.append(
            CaseResult(
                case_id=f"quality/{name}-grounded",
                tier=Tier.QUALITY,
                score=1.0 if drafted.safe_to_send else 0.0,
                passed=drafted.safe_to_send,
                detail="; ".join(issues[:3]),
                metrics={
                    "drafts": float(len(drafted.drafts)),
                    "repairs": float(drafted.extraction.repairs),
                    "escalated": 1.0 if drafted.extraction.escalated else 0.0,
                },
            )
        )
    return results


# ------------------------------------------------------- tier 0, second half


@dataclass(frozen=True, slots=True)
class Mutation:
    """A plan broken in one specific way, and the violation that must be raised."""

    name: str
    why: str
    expect: ViolationCode
    apply: Callable[[WorldState, PlanVersion], PlanVersion]


def _swap_crew(worker_ids: tuple[str, ...]) -> Callable[..., PlanVersion]:
    def mutate(world: WorldState, plan: PlanVersion) -> PlanVersion:
        route = plan.routes[0]
        return plan.model_copy(
            update={"routes": (route.model_copy(update={"worker_ids": worker_ids}),)}
        )

    return mutate


def _duplicate_route(world: WorldState, plan: PlanVersion) -> PlanVersion:
    route = plan.routes[0]
    return plan.model_copy(update={"routes": (route, route.model_copy(update={"crew_id": "copy"}))})


def _teleport(world: WorldState, plan: PlanVersion) -> PlanVersion:
    """Claim an arrival no drive could achieve."""
    route = plan.routes[0]
    stop = route.stops[0]
    cheated = stop.model_copy(
        update={
            "arrival": stop.arrival - timedelta(hours=2),
            "departure": stop.departure - timedelta(hours=2),
            "travel_minutes_from_prev": 0,
        }
    )
    return plan.model_copy(
        update={"routes": (route.model_copy(update={"stops": (cheated, *route.stops[1:])}),)}
    )


def _rush_the_job(world: WorldState, plan: PlanVersion) -> PlanVersion:
    route = plan.routes[0]
    stop = route.stops[0]
    rushed = stop.model_copy(update={"departure": stop.arrival + timedelta(minutes=1)})
    return plan.model_copy(
        update={"routes": (route.model_copy(update={"stops": (rushed, *route.stops[1:])}),)}
    )


def _unknown_worker(world: WorldState, plan: PlanVersion) -> PlanVersion:
    route = plan.routes[0]
    return plan.model_copy(
        update={"routes": (route.model_copy(update={"worker_ids": ("w-nobody",)}),)}
    )


def _empty_van(world: WorldState, plan: PlanVersion) -> PlanVersion:
    """Send a crew in a van carrying nothing, to work that needs parts."""
    route = plan.routes[0]
    van = world.vans[route.van_id]
    world.vans[route.van_id] = van.model_copy(update={"stock": {}, "rack_slots": 1})
    return plan


def _before_the_shift(world: WorldState, plan: PlanVersion) -> PlanVersion:
    route = plan.routes[0]
    shifted = tuple(
        stop.model_copy(
            update={
                "arrival": stop.arrival - timedelta(hours=6),
                "departure": stop.departure - timedelta(hours=6),
            }
        )
        for stop in route.stops
    )
    return plan.model_copy(update={"routes": (route.model_copy(update={"stops": shifted}),)})


#: Each mutation is a plan a correct checker must reject. Without these, tier 0 only
#: proves the *solver* is right: a checker that returned no violations at all would
#: score a hundred percent, because a missing check produces a missing violation
#: rather than a detected one. That is exactly what happened the first time this gate
#: was tried against a deliberately weakened checker - it passed.
MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "uncertified-crew",
        "a crew sent to work it is not certified for",
        ViolationCode.MISSING_CERTIFICATION,
        _swap_crew(("w-sofia",)),
    ),
    Mutation(
        "duplicate-route",
        "the same crew and the same job twice over",
        ViolationCode.DUPLICATE_JOB_ASSIGNMENT,
        _duplicate_route,
    ),
    Mutation(
        "teleporting-crew",
        "an arrival two hours before any drive could deliver it",
        ViolationCode.TRAVEL_TIME_INCONSISTENT,
        _teleport,
    ),
    Mutation(
        "one-minute-job",
        "a two-hour install allotted one minute on site",
        ViolationCode.TRAVEL_TIME_INCONSISTENT,
        _rush_the_job,
    ),
    Mutation(
        "phantom-worker",
        "a route crewed by somebody who does not exist",
        ViolationCode.UNKNOWN_ENTITY_REFERENCE,
        _unknown_worker,
    ),
    Mutation(
        "empty-van",
        "a van with no stock and one rack slot sent to a day of work",
        ViolationCode.VAN_CAPACITY_EXCEEDED,
        _empty_van,
    ),
    Mutation(
        "before-the-shift",
        "a crew working six hours before anyone clocks on",
        ViolationCode.OUTSIDE_WORKING_HOURS,
        _before_the_shift,
    ),
)


def run_checker_detection() -> list[CaseResult]:
    """Prove the checker still catches what it is supposed to catch.

    The other half of tier 0. Solving-and-validating shows the solver agrees with the
    checker; this shows the checker would object if it did not.
    """
    business = _business()
    travel = build_travel(business, TravelMode.FROZEN)
    params = SolveParams.from_business(business, _tz())
    config = ValidationConfig(business_tz=_tz())
    results: list[CaseResult] = []

    for mutation in MUTATIONS:
        # Each mutation gets a fresh world: some of them alter it, and a leaked change
        # would silently weaken the next case.
        world = scenario_library.get("baseline").world()
        day = plan_day(
            world=world,
            travel=travel,
            on_date=WEEK_START,
            candidate_job_ids=[j.id for j in world.active_jobs()],
            params=params,
        )
        if not day.routes:
            continue

        plan = PlanVersion(
            id=f"mut-{mutation.name}",
            created_at=world.as_of,
            horizon_start=WEEK_START,
            horizon_end=WEEK_START,
            routes=day.routes[:1],
        )
        broken = mutation.apply(world, plan)
        codes = {v.code for v in validate_plan(broken, world, travel, config)}
        caught = mutation.expect in codes

        results.append(
            CaseResult(
                case_id=f"detect/{mutation.name}",
                tier=Tier.INVARIANTS,
                score=1.0 if caught else 0.0,
                passed=caught,
                detail=(
                    ""
                    if caught
                    else (
                        f"{mutation.why} went unreported; expected "
                        f"{mutation.expect.value}, checker raised {sorted(c.value for c in codes)}"
                    )
                ),
            )
        )
    return results
