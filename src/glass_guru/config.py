"""Business parameters, loaded from YAML, each carrying its own provenance.

The objective in :mod:`glass_guru.scheduler.day_planner` is denominated in dollars
so that its weights are *arguable* rather than arbitrary tuning constants. That
argument only holds if the numbers are real. Today almost none of them are - they
are plausible inventions - and burying them as defaults in a dataclass hides that.

So every rate lives here with a ``source`` tag, and ``glass-guru params`` prints the
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


class PenaltyParams(Section):
    unserved_base: Param
    deferral_escalation: Param
    lateness_per_minute: Param
    revenue_weight: Param


class TravelParams(Section):
    road_factor: Param
    approach_minutes: Param
    traffic_multipliers: dict[str, Param]


class ServiceAreaParams(Section):
    radius_miles: Param


class SolverParams(Section):
    max_solve_seconds: Param
    search_workers: Param
    random_seed: Param


class BusinessMeta(Section):
    name: str
    timezone: str
    currency: str = "USD"


class BusinessParams(Section):
    """Everything the planner needs that is a business fact rather than a code fact."""

    meta: BusinessMeta
    labor: LaborParams
    vehicle: VehicleParams
    penalties: PenaltyParams
    travel: TravelParams
    service_area: ServiceAreaParams
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
