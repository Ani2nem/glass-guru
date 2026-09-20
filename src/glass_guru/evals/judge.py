"""Scoring the part of a customer message that cannot be checked.

Grounding is decided by a regular expression, house style by a list of rules, and
neither needs an opinion. What is left genuinely does: whether a message blames
somebody, whether it leaves the customer knowing what happens next, whether it treats
a twenty-minute delay like a twenty-minute delay. A model is the only practical way to
score that, and a model scoring another model's output is worth exactly nothing until
somebody checks it.

So the judge is not trusted by default. Before any tone score counts, it is run
against a labelled set and has to agree with it. If it does not, the tone cases fail
with that as the reason rather than reporting a number that means nothing - an
uncalibrated judge reporting 100% is worse than no judge at all, because it looks like
evidence.

The check has two halves, and the second is the one that matters:

  agreement    how often the judge matches the label
  discrimination  whether it ever says "poor" at all

A judge that answers "good" to everything scores 50% on a balanced set, which some
thresholds would let through, and is useless. Requiring it to produce both verdicts
catches that directly rather than hoping a percentage does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from glass_guru.agents.llm.base import LLMProvider
from glass_guru.agents.structured import Example, Extraction, extract

DATASET = Path(__file__).resolve().parent / "datasets" / "comms_tone.yaml"

#: Below this the judge is not trusted and tone is not scored. Set where it is because
#: the labelled cases are clear-cut by construction: a judge that reads the rubric and
#: applies it should get nearly all of them, and one that is guessing lands near 50%.
#: This is not a quality bar for the *messages* - it is a competence bar for the judge.
MIN_AGREEMENT = 0.80


class ToneVerdict(BaseModel):
    """One judgement. Deliberately small - a long rubric makes a small model worse."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["good", "poor"] = Field(
        description="good if you would be content to receive this message, poor if not."
    )
    reason: str = Field(description="One short sentence. What decided it.")


SYSTEM = """\
You judge messages sent by a glass-fitting business to its customers when an
appointment changes. Answer with one verdict and one short reason.

Say "poor" if the message does any of these:
- blames the customer for something the business caused
- names an employee and blames them
- says something has changed without saying what to do about it
- makes a routine change sound alarming or dramatic
- buries the actual change under corporate padding
- is so abrupt that it reads as rude

Say "good" otherwise. A short message is not poor for being short. Admitting the
business does not yet know a time is good, not poor - guessing would be worse.
Apologising once is fine.
"""


EXAMPLES: tuple[Example, ...] = (
    Example(
        text=(
            "Hi - your appointment is moving to Thursday. If you'd been in when we came "
            "last time this wouldn't have been necessary."
        ),
        output=ToneVerdict(
            verdict="poor", reason="Blames the customer for a delay the business caused."
        ),
    ),
    Example(
        text=(
            "Hi Sarah - one of our vans is off the road, so we need to move your fitting "
            "to today at 12:40. Sorry about that. Call us if that doesn't work."
        ),
        output=ToneVerdict(
            verdict="good", reason="Says what changed, when, and what the customer can do."
        ),
    ),
)


def judge_tone(provider: LLMProvider, body: str) -> Extraction[ToneVerdict]:
    """One message, one verdict. Same validate-and-repair path as every other call."""
    return extract(
        provider,
        ToneVerdict,
        system=SYSTEM,
        text=body,
        examples=EXAMPLES,
    )


@dataclass(frozen=True, slots=True)
class LabelledMessage:
    id: str
    verdict: str
    why: str
    body: str


@dataclass(frozen=True, slots=True)
class Calibration:
    """How well the judge matched the labels, and whether it may be used."""

    agreed: int
    total: int
    disagreements: tuple[str, ...]
    verdicts_produced: frozenset[str]

    @property
    def agreement(self) -> float:
        return self.agreed / self.total if self.total else 0.0

    @property
    def discriminates(self) -> bool:
        """Did it ever say "poor"? A judge with one answer has no opinion."""
        return len(self.verdicts_produced) > 1

    @property
    def trustworthy(self) -> bool:
        return self.agreement >= MIN_AGREEMENT and self.discriminates

    @property
    def summary(self) -> str:
        if not self.total:
            return "no labelled cases"
        if not self.discriminates:
            only = next(iter(self.verdicts_produced), "nothing")
            return f"judge answered '{only}' to all {self.total} cases - it has no opinion"
        base = f"agreed with {self.agreed}/{self.total} labels ({self.agreement:.0%})"
        if self.disagreements:
            return f"{base}; missed {', '.join(self.disagreements[:3])}"
        return base


def load_labels(path: Path | None = None) -> tuple[LabelledMessage, ...]:
    raw = yaml.safe_load((path or DATASET).read_text())
    return tuple(
        LabelledMessage(
            id=case["id"],
            verdict=case["verdict"],
            why=case.get("why", ""),
            body=" ".join(case["body"].split()),
        )
        for case in raw["cases"]
    )


def calibrate(provider: LLMProvider, path: Path | None = None) -> Calibration:
    """Run the judge over the labelled set and report whether it can be believed."""
    labels = load_labels(path)
    agreed = 0
    missed: list[str] = []
    produced: set[str] = set()

    for case in labels:
        result = judge_tone(provider, case.body)
        if result.value is None:
            missed.append(f"{case.id} (no verdict)")
            continue
        produced.add(result.value.verdict)
        if result.value.verdict == case.verdict:
            agreed += 1
        else:
            missed.append(f"{case.id} (said {result.value.verdict}, labelled {case.verdict})")

    return Calibration(
        agreed=agreed,
        total=len(labels),
        disagreements=tuple(missed),
        verdicts_produced=frozenset(produced),
    )
