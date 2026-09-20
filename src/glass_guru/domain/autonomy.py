"""When may the system change the plan on its own?

Deterministic code, deliberately not a prompt. A language model deciding whether an
appointment may be moved silently is the wrong division of labour: the question is
checkable, so it belongs on the checkable side of the line. The coordinator agent
chooses *strategy* - which trade-off to make - and this decides whether the result
needs a human.

The structural rules come first and cannot be bought off by a cost threshold. A
change that moves a promised window is escalated no matter how cheap it is, because
the cost of getting that wrong is not measured in dollars. Thresholds only bound how
much silent internal churn is acceptable.

Both failure modes are real. Escalate too much and the dispatcher stops reading the
queue within a week; escalate too little and the system moves an appointment somebody
booked a day off work for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from glass_guru.config import BusinessParams
from glass_guru.domain.diff import BlastRadius, PlanDiff


class Decision(StrEnum):
    AUTO_APPLY = "auto_apply"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    max_cost_delta: float = 150.0
    max_changes: int = 12
    #: Overtime is a spend commitment against a worker's evening. Even when no
    #: customer notices, somebody should agree to it.
    escalate_new_overtime: bool = True

    @classmethod
    def from_business(cls, business: BusinessParams) -> AutonomyPolicy:
        return cls(
            max_cost_delta=business.autonomy.max_auto_cost_delta.value,
            max_changes=int(business.autonomy.max_auto_changes.value),
        )


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    decision: Decision
    reasons: tuple[str, ...]

    @property
    def auto(self) -> bool:
        return self.decision is Decision.AUTO_APPLY

    def explain(self) -> str:
        verb = "applied automatically" if self.auto else "needs a dispatcher"
        if not self.reasons:
            return verb
        return f"{verb}: " + "; ".join(self.reasons)


def decide(
    diff: PlanDiff,
    policy: AutonomyPolicy,
    *,
    added_overtime_minutes: int = 0,
) -> AutonomyDecision:
    """Whether this change may be applied without asking."""
    blocking: list[str] = []

    if diff.blast_radius is BlastRadius.CUSTOMER_VISIBLE:
        visible = diff.customer_visible_changes
        names = ", ".join(c.customer_name or c.job_id for c in visible[:3])
        blocking.append(f"{len(visible)} customer(s) would need telling ({names})")

    if policy.escalate_new_overtime and added_overtime_minutes > 0:
        blocking.append(f"adds {added_overtime_minutes} min of overtime")

    if diff.cost_delta > policy.max_cost_delta:
        blocking.append(
            f"costs ${diff.cost_delta:,.2f} more, over the ${policy.max_cost_delta:,.2f} limit"
        )

    if len(diff.changes) > policy.max_changes:
        blocking.append(f"{len(diff.changes)} movements, over the limit of {policy.max_changes}")

    if blocking:
        return AutonomyDecision(Decision.ESCALATE, tuple(blocking))

    if not diff.changes:
        return AutonomyDecision(Decision.AUTO_APPLY, ("nothing changed",))
    return AutonomyDecision(
        Decision.AUTO_APPLY,
        (f"{len(diff.changes)} internal change(s), no promised window moved",),
    )
