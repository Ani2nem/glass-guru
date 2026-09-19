"""Golden snapshots of the rendered board, one per scenario.

These assert nothing about correctness - the invariant tests do that. Their job is to
turn "the plan quietly changed" into a reviewable diff. The bugs that motivated them
(travel labour charged per crew instead of per person; other-day jobs reported as a
capacity failure) were all valid-but-wrong: every existing test passed, and the only
way to notice was to look at the output. A committed snapshot means the next such
change shows up in `git diff` instead of going unnoticed.

Regenerate deliberately, never reflexively:  make snapshots
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from glass_guru.cli.main import _render_horizon, _render_solve
from glass_guru.config import BusinessParams
from glass_guru.fixtures import scenarios

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

#: Wall-clock solve time varies run to run and says nothing about the plan.
_TIMING = re.compile(r"\d+\.\d{3}s")


def normalize(board: str) -> str:
    return _TIMING.sub("N.NNNs", board)


def render(name: str) -> str:
    scenario = scenarios.get(name)
    business = BusinessParams.load()
    board, _ = _render_solve(scenario.world(), business, scenario.solve_date)
    header = f"SCENARIO  {scenario.name}\n{' '.join(scenario.description.split())}\n\n"
    return normalize(header + board) + "\n"


def render_horizon_board() -> str:
    """The rolling five-day board, which exercises day assignment as well as routing."""
    scenario = scenarios.get("baseline")
    business = BusinessParams.load()
    board, _ = _render_horizon(scenario.world(), business, scenario.solve_date)
    return normalize(board) + "\n"


def test_horizon_board_matches_snapshot() -> None:
    path = SNAPSHOT_DIR / "horizon.txt"
    actual = render_horizon_board()
    if UPDATE or not path.exists():
        path.write_text(actual)
        if not UPDATE:
            pytest.skip("created missing horizon snapshot; re-run to assert against it")
        return
    assert actual == path.read_text(), (
        "rendered horizon board differs from tests/snapshots/horizon.txt.\n"
        "If the change is intended, review the diff and run: make snapshots"
    )


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_scenario_board_matches_snapshot(name: str) -> None:
    path = SNAPSHOT_DIR / f"{name}.txt"
    actual = render(name)

    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        if not UPDATE:
            pytest.skip(f"created missing snapshot {path.name}; re-run to assert against it")
        return

    expected = path.read_text()
    assert actual == expected, (
        f"rendered board for {name!r} differs from tests/snapshots/{name}.txt.\n"
        "If the change is intended, review the diff and run: make snapshots"
    )


def test_every_scenario_is_snapshotted() -> None:
    """A new scenario without a committed snapshot is a silent gap."""
    missing = [n for n in scenarios.SCENARIOS if not (SNAPSHOT_DIR / f"{n}.txt").exists()]
    assert not missing, f"scenarios with no snapshot: {missing}"
