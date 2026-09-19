"""CLI behaviour and the helpers behind the partial-outage and matrix-probe fixes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from glass_guru.cli.main import main
from glass_guru.config import BusinessParams, Provenance
from glass_guru.domain.state import Unavailability
from glass_guru.fixtures.sample_business import _at
from glass_guru.scheduler.day_planner import (
    MINUTES_PER_DAY,
    _largest_free_interval,
    _probe_times,
)
from glass_guru.scheduler.travel.base import TimeBucket

DAY = _at(0, 0)


def free(outages: list[Unavailability], lo: int = 0, hi: int = MINUTES_PER_DAY):
    return _largest_free_interval(outages, DAY, lo, hi)


def outage(from_hour: float, to_hour: float | None) -> Unavailability:
    return Unavailability(
        from_time=DAY + timedelta(hours=from_hour),
        until_time=None if to_hour is None else DAY + timedelta(hours=to_hour),
    )


# ------------------------------------------------------------- partial availability


def test_no_outage_leaves_the_whole_shift():
    assert free([], 480, 1020) == (480, 1020)


def test_midday_breakdown_keeps_the_longer_side():
    """A van that dies at 10:40 was usable all morning. Writing off the whole day
    throws away real capacity for no reason."""
    assert free([outage(10.0, None)], 360, 1020) == (360, 600)


def test_early_outage_keeps_the_afternoon():
    assert free([outage(0, 10)], 480, 1020) == (600, 1020)


def test_outage_covering_the_shift_yields_nothing():
    assert free([outage(0, 24)], 480, 1020) is None


def test_outage_outside_the_shift_is_ignored():
    assert free([outage(20, 23)], 480, 1020) == (480, 1020)


def test_overlapping_outages_are_merged():
    assert free([outage(9, 12), outage(11, 13)], 480, 1020) == (780, 1020)


def test_two_gaps_returns_the_larger():
    """08:00-09:00 is one hour; 12:00-17:00 is five. Take the five."""
    assert free([outage(9, 12)], 480, 1020) == (720, 1020)


# --------------------------------------------------------------- travel-matrix probes


def test_probes_cover_every_bucket_the_shift_touches():
    """A single probe time makes the matrix blind to congestion at other hours -
    including a TrafficDelay event scoped to a window the probe never samples."""
    probes = _probe_times(DAY, 6 * 60, 19 * 60, None)
    hours = sorted(p.hour for p in probes)
    assert hours == [6, 8, 12, 16, 19]


def test_short_shift_probes_fewer_buckets():
    probes = _probe_times(DAY, 10 * 60, 14 * 60, None)
    assert [p.hour for p in probes] == [12]


def test_forced_bucket_probes_once():
    probes = _probe_times(DAY, 6 * 60, 19 * 60, TimeBucket.AM_PEAK)
    assert [p.hour for p in probes] == [8]


# -------------------------------------------------------------------------- commands


@pytest.mark.parametrize("argv", [["show"], ["params"], ["scenario", "list"], ["solve"]])
def test_commands_succeed(argv, capsys):
    assert main(argv) == 0
    assert capsys.readouterr().out.strip()


def test_solve_reports_zero_violations(capsys):
    main(["solve", "--date", "2026-09-21"])
    assert "invariants: 0 violations" in capsys.readouterr().out


def test_explain_covers_scheduled_and_unserved(capsys):
    main(["explain", "j-401"])
    assert "SCHEDULED" in capsys.readouterr().out
    main(["explain", "j-406"])
    assert "NOT SCHEDULED" in capsys.readouterr().out


def test_explain_unknown_job_exits_nonzero(capsys):
    assert main(["explain", "j-does-not-exist"]) == 2


def test_unknown_scenario_exits_nonzero(capsys):
    assert main(["scenario", "not-a-scenario"]) == 2
    assert "known scenarios" in capsys.readouterr().err


def test_board_warns_while_parameters_are_guesses(capsys):
    """Every cost on the board is built on unvalidated numbers. That must be visible,
    not something a reader has to go looking for."""
    main(["solve"])
    assert "still estimates" in capsys.readouterr().out


# --------------------------------------------------------------------------- config


def test_every_parameter_has_a_note():
    """A number without a note is a number nobody can audit later."""
    missing = [path for path, param in BusinessParams.load().walk() if not param.note]
    assert not missing, f"parameters lacking a note: {missing}"


def test_config_is_currently_all_estimates():
    """Guards the honesty of the banner. When real figures land, update this test
    deliberately - it should never drift silently."""
    assert BusinessParams.load().weakest_source() is Provenance.ESTIMATED
