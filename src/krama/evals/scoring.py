"""Comparing what a model extracted against what it should have.

Field-level rather than whole-object. "The extraction was wrong" is not a finding;
"it got the phone number but invented a pane count" is. When a tier drops, the report
has to say which field moved, because that is what determines whether the fix is a
prompt, a schema, or a different model.

Fields are weighted, because they are not equally consequential. A wrong phone number
means a customer cannot be reached; a differently-worded site note means nothing. Left
unweighted, a change in how the model phrases free text would move the score as much
as losing the address, and the number would stop meaning anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Match(StrEnum):
    """How a field is compared."""

    #: Normalised string equality. For enums, ids, and anything a schema constrains.
    EXACT = "exact"
    #: Digits only. A phone number written three ways is the same phone number.
    DIGITS = "digits"
    #: Set overlap, scored by F1. For certifications, signals, tags.
    SET = "set"
    #: Numeric, within a tolerance.
    NUMERIC = "numeric"
    #: Both present or both absent. For free text, where exact wording is not the point
    #: but silently dropping the field is.
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    match: Match = Match.EXACT
    #: Relative importance. A wrong callback number is not a wrong site note.
    weight: float = 1.0
    tolerance: float = 0.0
    #: Skip when the gold label leaves it blank - "not stated on the call" is not a
    #: thing the model can be marked down for failing to invent.
    optional: bool = True


@dataclass(frozen=True, slots=True)
class FieldOutcome:
    name: str
    score: float
    weight: float
    gold: Any
    actual: Any

    @property
    def correct(self) -> bool:
        return self.score >= 0.999

    def describe(self) -> str:
        return f"{self.name}: expected {self.gold!r}, got {self.actual!r}"


@dataclass(frozen=True, slots=True)
class FieldScore:
    outcomes: tuple[FieldOutcome, ...] = field(default_factory=tuple)

    @property
    def score(self) -> float:
        total = sum(o.weight for o in self.outcomes)
        if total == 0:
            return 1.0
        return sum(o.score * o.weight for o in self.outcomes) / total

    @property
    def wrong(self) -> tuple[FieldOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.correct)

    def summary(self, limit: int = 3) -> str:
        if not self.wrong:
            return "all fields correct"
        parts = [o.describe() for o in self.wrong[:limit]]
        if len(self.wrong) > limit:
            parts.append(f"… {len(self.wrong) - limit} more")
        return "; ".join(parts)


def _normalise(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _as_set(value: Any) -> set[str]:
    """Anything as a set of normalised strings.

    Scalars become a one-element set rather than being iterated. A number reaching
    here used to raise - `pane_count` is an int, and `_as_set(2)` tried to walk it.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {_normalise(value)} if value.strip() else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {_normalise(v) for v in value if str(v).strip()}
    return {_normalise(value)}


def _is_blank(value: Any) -> bool:
    """Whether a gold label said nothing about this field.

    Zero and False are values, not silence: a job with zero panes stated is different
    from a call that never mentioned panes.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def _f1(gold: set[str], actual: set[str]) -> float:
    """Set overlap. Both empty is a correct answer, not a division by zero."""
    if not gold and not actual:
        return 1.0
    if not gold or not actual:
        return 0.0
    overlap = len(gold & actual)
    if overlap == 0:
        return 0.0
    precision = overlap / len(actual)
    recall = overlap / len(gold)
    return 2 * precision * recall / (precision + recall)


def _numeric(gold: Any, actual: Any, tolerance: float) -> float:
    try:
        expected, got = float(gold), float(actual)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 if abs(expected - got) <= tolerance else 0.0


def score_field(spec: FieldSpec, gold: Any, actual: Any) -> FieldOutcome:
    match spec.match:
        case Match.EXACT:
            score = 1.0 if _normalise(gold) == _normalise(actual) else 0.0
        case Match.DIGITS:
            score = 1.0 if _digits(gold) == _digits(actual) else 0.0
        case Match.SET:
            score = _f1(_as_set(gold), _as_set(actual))
        case Match.NUMERIC:
            score = _numeric(gold, actual, spec.tolerance)
        case Match.PRESENCE:
            score = 1.0 if bool(_normalise(gold)) == bool(_normalise(actual)) else 0.0
    return FieldOutcome(name=spec.name, score=score, weight=spec.weight, gold=gold, actual=actual)


def score_fields(
    gold: dict[str, Any], actual: dict[str, Any], specs: Sequence[FieldSpec]
) -> FieldScore:
    """Score one extraction against its gold label.

    Optional fields the gold label leaves blank are skipped entirely rather than
    scored as correct. Counting them as passes inflates every score by however many
    details a particular call happened not to mention, which makes an easy case look
    like a good model.
    """
    outcomes: list[FieldOutcome] = []
    for spec in specs:
        expected = gold.get(spec.name)
        if spec.optional and _is_blank(expected):
            continue
        outcomes.append(score_field(spec, expected, actual.get(spec.name)))
    return FieldScore(outcomes=tuple(outcomes))


#: What matters when reading a call. Weighted by what goes wrong if it is missed:
#: no phone number means no callback; a mis-heard site note means nothing.
CALL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("customer_name", Match.EXACT, weight=2.0),
    FieldSpec("phone", Match.DIGITS, weight=3.0),
    FieldSpec("address", Match.PRESENCE, weight=3.0),
    FieldSpec("service_type", Match.EXACT, weight=4.0),
    FieldSpec("pane_count", Match.NUMERIC, weight=2.0),
    FieldSpec("property_type", Match.EXACT, weight=1.0),
    FieldSpec("urgency", Match.EXACT, weight=2.0),
    # The field the whole commitment design rests on. A missed signal means a promise
    # the optimiser will move for free.
    FieldSpec("commitment_signals", Match.SET, weight=4.0),
    FieldSpec("hard_constraint", Match.PRESENCE, weight=2.0),
    FieldSpec("glass_type", Match.EXACT, weight=1.0),
    FieldSpec("site_notes", Match.PRESENCE, weight=0.5),
)

#: Triage is narrower: the events are the answer.
TRIAGE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("event_kinds", Match.SET, weight=4.0, optional=False),
    FieldSpec("targets", Match.SET, weight=4.0, optional=False),
    FieldSpec("asks_question", Match.EXACT, weight=2.0, optional=False),
)
