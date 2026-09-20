"""Coordinator: choosing between priced options, not authoring a schedule.

This is the agent most likely to be done badly, so the shape of the task is the
design. The coordinator does not plan, does not set times, and does not decide what
may be applied without a human. It reads several candidate repairs the solver has
already priced and picks one, with a sentence a dispatcher can read.

Ranking a short list against stated priorities is well within a small model's range.
Open-ended planning is not, and asking for it would produce something confident and
unverifiable. Constraining the task is not a limitation worked around - it is the
reason the agent can be trusted at all.

Two guards make the choice safe regardless of what the model returns. Its answer is
an enum, so an unrecognised strategy fails validation rather than propagating. And
whatever it picks, the deterministic autonomy policy still decides whether the result
may be applied silently; the model has no say in that and no way to acquire one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.structured import Example, Extraction, extract
from glass_guru.domain.autonomy import AutonomyDecision, AutonomyPolicy, decide
from glass_guru.obs.tracing import record, span
from glass_guru.scheduler.repair import RepairCandidate, RepairOptions

StrategyName = Literal["least_disruption", "most_jobs", "no_overtime"]


class CoordinatorChoice(BaseModel):
    """Which trade-off to take, and why."""

    model_config = ConfigDict(extra="forbid")

    chosen_strategy: StrategyName = Field(
        description="The strategy that best fits the priorities, by name."
    )
    rationale: str = Field(
        description=(
            "One sentence a dispatcher can read, naming the trade-off actually made. No preamble."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="How clear-cut this was. Use low when the options are close."
    )
    wants_human_review: bool = Field(
        default=False,
        description=(
            "True when a person should look at this even if the rules would allow it "
            "to apply automatically."
        ),
    )


SYSTEM = """\
You are a dispatcher's assistant at a glass-fitting business. A van or a worker has
gone down, and the scheduling engine has produced several repaired plans. Each has
already been costed and checked; your only job is to choose between them.

Priorities, in order:
1. Keep promises. A customer who was given a window and has arranged their day around
   it is worth more than an extra job.
2. Serve as much work as possible.
3. Avoid overtime where the difference is small.

Choose by name from the strategies offered. Do not invent a strategy, propose a
schedule, or suggest times. If the options are close, say so with low confidence and
set wants_human_review.
"""


def _describe(candidate: RepairCandidate) -> str:
    """A compact, factual summary. Every extra line is a chance to weigh the wrong one."""
    visible = candidate.diff.customer_visible_changes
    lines = [
        f"- {candidate.strategy.name}: {candidate.strategy.description}",
        f"  jobs served: {candidate.jobs_served}",
        f"  changes: {candidate.changes}",
        f"  customers who must be phoned: {candidate.customer_calls}",
        f"  reach: {candidate.diff.blast_radius.value}",
    ]
    lines += [f"  would phone: {c.customer_name or c.job_id}" for c in visible[:3]]
    return "\n".join(lines)


EXAMPLES: tuple[Example, ...] = (
    Example(
        text=(
            "- least_disruption: Keep every promise.\n"
            "  jobs served: 7\n  changes: 3\n  customers who must be phoned: 0\n"
            "  reach: internal\n"
            "- most_jobs: Serve the most work today.\n"
            "  jobs served: 9\n  changes: 14\n  customers who must be phoned: 2\n"
            "  reach: customer_visible\n  would phone: Chen Residence"
        ),
        output=CoordinatorChoice(
            chosen_strategy="least_disruption",
            rationale=(
                "Two extra jobs are not worth breaking two promises and fourteen "
                "changes; keeping the day intact costs us less than the phone calls."
            ),
            confidence="high",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CoordinatorDecision:
    """The chosen candidate, the reason, and what the rules independently allow."""

    candidate: RepairCandidate | None
    rationale: str
    confidence: str
    autonomy: AutonomyDecision | None
    #: True when the model picked something other than the engine's default. Worth
    #: watching: a model that always agrees is adding nothing, and one that always
    #: disagrees is a problem.
    overrode_default: bool
    extraction: Extraction[CoordinatorChoice]

    @property
    def needs_human(self) -> bool:
        """Policy first, model second.

        The deterministic rules decide; the model may only ask for *more* review, never
        less. A model that could talk its way into applying a customer-visible change
        silently would make the whole autonomy layer decorative.
        """
        if self.autonomy is None or self.candidate is None:
            return True
        if not self.autonomy.auto:
            return True
        return bool(self.extraction.value and self.extraction.value.wants_human_review)


def choose_repair(
    provider: LLMProvider,
    options: RepairOptions,
    policy: AutonomyPolicy,
    *,
    max_attempts: int = 3,
) -> CoordinatorDecision:
    """Pick a repair candidate. Falls back to the engine's default if the model cannot."""
    default = options.best_by_fewest_calls

    if not options.candidates:
        return CoordinatorDecision(
            candidate=None,
            rationale="the engine produced no repair candidates",
            confidence="low",
            autonomy=None,
            overrode_default=False,
            extraction=Extraction(value=None),
        )

    with span("agent.coordinator", candidates=len(options.candidates)) as active:
        summary = "\n".join(_describe(c) for c in options.candidates)
        extraction = extract(
            provider,
            CoordinatorChoice,
            system=SYSTEM,
            text=f"Repair options for plan {options.baseline.id}:\n\n{summary}",
            examples=EXAMPLES,
            max_attempts=max_attempts,
            schema_description="The chosen strategy and the reason for it.",
        )

        choice = extraction.value
        if choice is None:
            # The model could not answer. The engine's own recommendation stands, and
            # the dispatcher is told the model abstained rather than being told nothing.
            record(escalated=True, fell_back=True)
            active.set_attribute("outcome", "fallback")
            return CoordinatorDecision(
                candidate=default,
                rationale=(
                    "the assistant could not choose, so the engine's own recommendation stands"
                ),
                confidence="low",
                autonomy=decide(default.diff, policy) if default else None,
                overrode_default=False,
                extraction=extraction,
            )

        by_name = {c.strategy.name: c for c in options.candidates}
        chosen = by_name.get(choice.chosen_strategy) or default
        autonomy = decide(chosen.diff, policy) if chosen else None

        record(
            chosen=choice.chosen_strategy,
            confidence=choice.confidence,
            repairs=extraction.repairs,
            overrode_default=chosen is not default,
            autonomy=autonomy.decision.value if autonomy else "",
        )
        active.set_attribute("outcome", "ok")

        return CoordinatorDecision(
            candidate=chosen,
            rationale=choice.rationale,
            confidence=choice.confidence,
            autonomy=autonomy,
            overrode_default=chosen is not default,
            extraction=extraction,
        )
