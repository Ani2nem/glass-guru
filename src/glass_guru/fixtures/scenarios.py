"""Named disruption scenarios, for looking at what the solver does under stress.

These are development tooling, not scored evals - their job is to make a change in
routing or cost visible as a reviewable diff. Each one is a deterministic list of
events appended to the sample business, so `glass-guru scenario <name>` always
renders exactly the same board until the engine itself changes.

They are also where "technically valid but obviously wrong" gets caught. The
invariant checker can prove a plan is feasible; it cannot tell you that losing one
van cost you four jobs when it should have cost one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from glass_guru.domain.events import (
    Event,
    JobCancelled,
    JobConfirmed,
    JobOverran,
    TrafficDelay,
    VanUnavailable,
    WorkerUnavailable,
)
from glass_guru.domain.models import TimeWindow
from glass_guru.domain.state import WorldState, fold
from glass_guru.fixtures.sample_business import WEEK_START, _at, seed_events


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    events: tuple[Event, ...] = ()
    solve_date: date = WEEK_START
    as_of: datetime = field(default_factory=lambda: _at(0, 12))

    def world(self) -> WorldState:
        return fold([*seed_events(), *self.events], as_of=self.as_of)


def _van_down(van_id: str, hour: int, minute: int, reason: str) -> VanUnavailable:
    broke = _at(0, hour, minute)
    return VanUnavailable(
        event_id=f"sc-van-{van_id}",
        occurred_at=broke,
        recorded_at=broke,
        dispatch_id="scenario",
        van_id=van_id,
        from_time=broke,
        reason=reason,
    )


def _worker_out(worker_id: str, reason: str) -> WorkerUnavailable:
    return WorkerUnavailable(
        event_id=f"sc-worker-{worker_id}",
        occurred_at=_at(0, 6),
        recorded_at=_at(0, 6),
        dispatch_id="scenario",
        worker_id=worker_id,
        from_time=_at(0, 0),
        until_time=_at(1, 0),
        reason=reason,
    )


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        name="baseline",
        description="An ordinary Monday with nothing going wrong.",
    ),
    "van_breakdown": Scenario(
        name="van_breakdown",
        description=(
            "Van 2 will not start from 10:40. The morning storefront job already "
            "used it, so the useful question is how much of the afternoon is lost - "
            "not whether the van is written off for the whole day."
        ),
        events=(_van_down("van-2", 10, 40, "wont start, tow called"),),
    ),
    "worker_sick": Scenario(
        name="worker_sick",
        description="Ken calls in sick. He is the only worker who cannot do overtime.",
        events=(_worker_out("w-ken", "sick"),),
    ),
    "commercial_crew_out": Scenario(
        name="commercial_crew_out",
        description=(
            "Both commercial-certified workers are out. The storefront job becomes "
            "unstaffable, and the dispatcher should be told that precisely rather "
            "than being told the day was full."
        ),
        events=(_worker_out("w-marcus", "sick"), _worker_out("w-priya", "family leave")),
    ),
    "job_overruns": Scenario(
        name="job_overruns",
        description="The shower-door install runs two hours over while the crew is on site.",
        events=(
            JobOverran(
                event_id="sc-overrun",
                occurred_at=_at(0, 11),
                recorded_at=_at(0, 11),
                dispatch_id="scenario",
                job_id="j-403",
                extra_minutes=120,
            ),
        ),
    ),
    "customer_cancels": Scenario(
        name="customer_cancels",
        description="The Chen job cancels at the top of the route, freeing a residential slot.",
        events=(
            JobCancelled(
                event_id="sc-cancel",
                occurred_at=_at(0, 8),
                recorded_at=_at(0, 8),
                dispatch_id="scenario",
                job_id="j-402",
                reason="customer cancelled",
            ),
        ),
    ),
    "promise_broken": Scenario(
        name="promise_broken",
        description=(
            "Chen was given a nine-to-half-eleven window and arranged her morning "
            "around it. Everyone who could do the work is then unavailable until "
            "half ten, so the promise cannot be kept. The only scenario where a "
            "customer has to be telephoned - without one, the comms agent is never "
            "exercised and the quality tier scores nothing."
        ),
        events=(
            JobConfirmed(
                event_id="sc-confirm",
                occurred_at=_at(0, 7),
                recorded_at=_at(0, 7),
                dispatch_id="scenario",
                job_id="j-402",
                window=TimeWindow(start=_at(0, 9), end=_at(0, 11, 30)),
                commitment_cost=250.0,
            ),
            *(
                WorkerUnavailable(
                    event_id=f"sc-late-{worker}",
                    occurred_at=_at(0, 6),
                    recorded_at=_at(0, 6),
                    dispatch_id="scenario",
                    worker_id=worker,
                    from_time=_at(0, 0),
                    until_time=_at(0, 10, 30),
                    reason="held up",
                )
                for worker in ("w-marcus", "w-priya", "w-dan", "w-ken")
            ),
        ),
    ),
    "traffic_spike": Scenario(
        name="traffic_spike",
        description="A corridor-wide slowdown makes every leg 80% longer all morning.",
        events=(
            TrafficDelay(
                event_id="sc-traffic",
                occurred_at=_at(0, 7),
                recorded_at=_at(0, 7),
                dispatch_id="scenario",
                multiplier=1.8,
                from_time=_at(0, 7),
                until_time=_at(0, 12),
                note="I-5 southbound incident",
            ),
        ),
    ),
}


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        known = ", ".join(sorted(SCENARIOS))
        raise KeyError(f"unknown scenario {name!r}; known scenarios: {known}") from None
