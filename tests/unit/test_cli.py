"""CLI behaviour and the helpers behind the partial-outage and matrix-probe fixes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from glass_guru.cli.main import main
from glass_guru.config import BusinessParams, Provenance
from glass_guru.domain.models import PlanVersion
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


# ------------------------------------------------------- init, and the way out


def test_init_on_an_existing_workspace_is_not_an_error(tmp_path, capsys):
    """`git init` on an existing repository is not an error, and neither is this.

    The first version printed a refusal to stderr and exited 2, which stopped a
    copy-pasted walkthrough dead on its second run and never said what to do about it.
    The state the caller wanted is the state they have.
    """
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "init"]) == 0

    assert main(["--workspace", ws, "init"]) == 0
    out = capsys.readouterr().out
    assert "already initialised" in out
    assert "--force" in out, "saying no without saying how is the bug being fixed"


def test_init_force_starts_over(tmp_path):
    from glass_guru.persistence.log import Workspace

    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "init"])
    plan = PlanVersion(
        id="v1",
        created_at=_at(0, 6),
        horizon_start=_at(0, 0).date(),
        horizon_end=_at(0, 0).date(),
    )
    Workspace(ws).plans.commit(plan, expected_parent=None)
    assert Workspace(ws).plans.head() is not None

    assert main(["--workspace", ws, "init", "--force"]) == 0
    assert Workspace(ws).plans.head() is None, "the old history should be gone"
    assert len(Workspace(ws).events) == 20, "and a fresh business seeded"


def test_an_s3_workspace_is_not_turned_into_a_directory():
    """Regression: `Path("s3://bucket/x")` quietly becomes `s3:/bucket/x`.

    The CLI wrapped its argument in Path before handing it over, so asking for the
    deployed workspace silently created a local folder with a URI for a name, seeded a
    business into it, and reported success. The deployed log was unreachable from the
    terminal and nothing said so.
    """
    from glass_guru.persistence.log import Workspace

    workspace = Workspace("s3://glass-guru-test/business")
    assert type(workspace.plans).__name__ == "S3PlanStore"
    assert type(workspace.events).__name__ == "S3EventLog"


def test_force_refuses_to_erase_a_deployed_workspace():
    """A convenience flag must not want a permission the runtime deliberately lacks.

    The deployed role has no s3:DeleteObject - the log is append-only and plan versions
    are immutable - and a `--force` that quietly needed one would be an argument for
    granting it.
    """
    from glass_guru.cli.main import _discard
    from glass_guru.persistence.log import Workspace

    with pytest.raises(SystemExit) as raised:
        _discard(Workspace("s3://glass-guru-test/business"))
    assert "refusing to erase" in str(raised.value)


def test_a_missing_credential_gets_a_remedy_rather_than_a_stack_trace():
    """The far more common of the two failures, and the one a trace helps least."""
    from botocore.exceptions import NoCredentialsError

    from glass_guru.cli.main import _remedy_for

    remedy = _remedy_for(NoCredentialsError())
    assert remedy is not None and "AWS_PROFILE" in remedy


def test_an_unrecognised_failure_still_raises():
    """Swallowing everything would turn a bug into a shrug."""
    from glass_guru.cli.main import _remedy_for

    assert _remedy_for(ValueError("something genuinely unexpected")) is None
