"""Business parameters, loaded from YAML, each carrying its own provenance.

The objective in :mod:`krama.scheduler.day_planner` is denominated in dollars
so that its weights are *arguable* rather than arbitrary tuning constants. That
argument only holds if the numbers are real. Today almost none of them are - they
are plausible inventions - and burying them as defaults in a dataclass hides that.

So every rate lives here with a ``source`` tag, and ``krama params`` prints the
lot. A number tagged ``estimated`` is a guess; ``measured`` means it came from the
business's own data; ``confirmed`` means a human there said it was right. Nothing
should reach production planning while the load-bearing weights are still guesses,
and this makes that visible instead of discoverable.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "business_params.yaml"


class Provenance(StrEnum):
    """Where a number came from. Ordered weakest to strongest."""

    ESTIMATED = "estimated"
    MEASURED = "measured"
    CONFIRMED = "confirmed"


class Param(BaseModel):
    """One tunable value plus the story of where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    source: Provenance = Provenance.ESTIMATED
    note: str = ""

    def __float__(self) -> float:
        return self.value


class Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LaborParams(Section):
    loaded_rate_per_minute: Param
    overtime_multiplier: Param
    overtime_max_minutes: Param


class VehicleParams(Section):
    cost_per_mile: Param


class SchedulingParams(Section):
    quoted_window_minutes: Param
    hard_window_buffer_minutes: Param


class CommitmentParams(Section):
    time_off_work: Param
    waiting_in: Param
    arranged_childcare: Param
    business_closed: Param
    already_rescheduled: Param
    max_total: Param


class AutonomyParams(Section):
    max_auto_cost_delta: Param
    max_auto_changes: Param


class PenaltyParams(Section):
    unserved_base: Param
    deferral_escalation: Param
    lateness_per_minute: Param
    revenue_weight: Param


class TravelParams(Section):
    road_factor: Param
    approach_minutes: Param
    traffic_multipliers: dict[str, Param]


class PricingParams(Section):
    """What the customer is charged. Separate from every cost parameter above it,
    because what a job costs to serve and what it sells for are different questions
    and conflating them is how a trade loses money on small work."""

    labour_rate_per_hour: Param
    call_out_fee: Param
    materials_markup: Param
    minimum_charge: Param
    after_hours_rate_multiplier: Param
    emergency_uplift: Param
    tax_rate: Param


class ServiceAreaParams(Section):
    radius_miles: Param


class HorizonParams(Section):
    days: Param
    day_capacity_utilization: Param
    sector_spread_penalty: Param
    day_delay_penalty: Param


class SolverParams(Section):
    max_solve_seconds: Param
    quote_solve_seconds: Param
    deterministic_budget: Param
    search_workers: Param
    random_seed: Param


class BusinessMeta(Section):
    name: str
    timezone: str
    currency: str = "USD"
    #: Service types this business actually sells. A fact, not an estimate, so it
    #: lives here rather than among the provenance-tracked parameters. Empty means
    #: "everything in the catalogue" - which keeps fixtures and tests that predate
    #: the field meaning what they always meant.
    services: list[str] = []
    #: The owner's PIN. Back-office configuration, not a UI setting: the person
    #: who edits this file is by definition the owner. Empty disables the role
    #: split entirely (single-user mode); the KRAMA_OWNER_PIN environment variable
    #: overrides it for deployments where the config file is public.
    owner_pin: str = ""

    def offers(self, service_type: str) -> bool:
        return not self.services or service_type in self.services


class BusinessParams(Section):
    """Everything the planner needs that is a business fact rather than a code fact."""

    meta: BusinessMeta
    labor: LaborParams
    vehicle: VehicleParams
    scheduling: SchedulingParams
    commitment: CommitmentParams
    autonomy: AutonomyParams
    penalties: PenaltyParams
    travel: TravelParams
    pricing: PricingParams
    service_area: ServiceAreaParams
    horizon: HorizonParams
    solver: SolverParams

    @classmethod
    def load(cls, path: Path | str | None = None) -> BusinessParams:
        resolved = Path(path) if path else DEFAULT_CONFIG_PATH
        with resolved.open() as handle:
            raw: dict[str, Any] = yaml.safe_load(handle)
        return cls.model_validate(raw)

    def walk(self) -> Iterator[tuple[str, Param]]:
        """Every parameter as ``(dotted.path, Param)``, for reporting."""
        yield from _walk(self, "")

    def weakest_source(self) -> Provenance:
        """The least-trustworthy provenance present. A plan is only as sound as this."""
        order = [Provenance.ESTIMATED, Provenance.MEASURED, Provenance.CONFIRMED]
        return min(
            (p.source for _, p in self.walk()), key=order.index, default=Provenance.CONFIRMED
        )

    def counts(self) -> dict[Provenance, int]:
        tally = dict.fromkeys(Provenance, 0)
        for _, param in self.walk():
            tally[param.source] += 1
        return tally


def _walk(model: BaseModel, prefix: str) -> Iterator[tuple[str, Param]]:
    for name, value in model:
        path = f"{prefix}{name}"
        if isinstance(value, Param):
            yield path, value
        elif isinstance(value, BaseModel):
            yield from _walk(value, f"{path}.")
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, Param):
                    yield f"{path}.{key}", item


class ParamsNotCalibrated(RuntimeWarning):
    """Raised as a warning when planning proceeds on unvalidated numbers."""


def calibration_banner(params: BusinessParams) -> str | None:
    """A one-line warning to print above any plan built on guesses, or ``None``."""
    tally = params.counts()
    guessed = tally[Provenance.ESTIMATED]
    if not guessed:
        return None
    total = sum(tally.values())
    return (
        f"{guessed} of {total} business parameters are still estimates. "
        "Costs below are internally consistent but not calibrated to this business."
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "BusinessParams",
    "Param",
    "Provenance",
    "calibration_banner",
]
