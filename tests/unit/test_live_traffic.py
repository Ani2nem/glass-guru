"""Live traffic incidents, and the seam they arrive through.

The point of this module is what it does *not* do. An incident does not reach into
the travel matrix; it becomes a TrafficOverride and takes the route a dispatcher's
"I-35 is backed up" already took, so the invariant checker, the plan diff and the
event log understand it without being told about a new concept.

None of these tests touch the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from glass_guru.domain.models import Location
from glass_guru.scheduler.travel.incidents import (
    Incident,
    NoIncidentFeed,
    ScriptedIncidentFeed,
    TomTomIncidentFeed,
    build_feed,
    overrides_from,
)

NOW = datetime.fromisoformat("2026-09-21T08:00:00-05:00")
ON_I35 = Location(lat=32.9666, lon=-97.2902, address="I-35W at Alliance")


def incident(severity: int, where: Location = ON_I35) -> Incident:
    return Incident(where=where, severity=severity, description="crash", started_at=NOW)


# ------------------------------------------------------------------ the default


def test_nothing_is_fetched_unless_a_feed_is_configured():
    """A live feed makes two solves of the same problem differ, which is what the
    frozen snapshot exists to prevent. Switching it on is a deployment decision."""
    assert isinstance(build_feed(), NoIncidentFeed)
    assert isinstance(build_feed("none"), NoIncidentFeed)
    assert build_feed().fetch(ON_I35, 30.0, NOW) == ()


def test_a_feed_that_needs_a_key_says_so(monkeypatch):
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TOMTOM_API_KEY"):
        build_feed("tomtom")


def test_an_unknown_feed_is_refused():
    with pytest.raises(ValueError, match="unknown traffic feed"):
        build_feed("waze")


# ---------------------------------------------------- incidents to overrides


def test_a_clear_road_produces_no_override():
    """Severity 0 is "unknown", not "slow". Treating it as a delay would have the
    planner rerouting around nothing."""
    assert overrides_from((incident(0),), NOW) == ()


@pytest.mark.parametrize(("severity", "expected"), [(1, 1.10), (2, 1.25), (3, 1.50), (4, 2.00)])
def test_severity_becomes_a_multiplier(severity: int, expected: float):
    overrides = overrides_from((incident(severity),), NOW)
    assert overrides and all(o.multiplier == expected for o in overrides)


def test_both_directions_are_slowed():
    """A blockage slows traffic that has to pass through the cell either way, so the
    corridor is scoped by origin and by destination rather than only one."""
    overrides = overrides_from((incident(3),), NOW)
    cell = ON_I35.geohash5
    assert {(o.origin_geohash5, o.dest_geohash5) for o in overrides} == {
        (cell, None),
        (None, cell),
    }


def test_an_incident_expires():
    """An override with no end would go on rerouting around a cleared crash forever."""
    overrides = overrides_from((incident(3),), NOW, default_duration=timedelta(hours=2))
    cell = ON_I35.geohash5
    for override in overrides:
        assert override.until_time == NOW + timedelta(hours=2)
        # Each override scopes one direction, so ask it about the one it covers.
        origin, dest = (cell, "other") if override.origin_geohash5 else ("other", cell)
        assert override.applies_to(origin, dest, NOW + timedelta(hours=1))
        assert not override.applies_to(origin, dest, NOW + timedelta(hours=3))


def test_a_stated_end_time_is_honoured():
    ends = NOW + timedelta(minutes=20)
    stated = Incident(where=ON_I35, severity=2, description="", started_at=NOW, ends_at=ends)
    assert all(o.until_time == ends for o in overrides_from((stated,), NOW))


# ------------------------------------------------------------------ parsing


def test_a_point_incident_parses():
    parsed = TomTomIncidentFeed._parse(
        {
            "geometry": {"type": "Point", "coordinates": [-97.29, 32.96]},
            "properties": {"magnitudeOfDelay": 3, "events": [{"description": "Overturned lorry"}]},
        },
        NOW,
    )
    assert (round(parsed.where.lat, 2), round(parsed.where.lon, 2)) == (32.96, -97.29)
    assert parsed.severity == 3
    assert parsed.description == "Overturned lorry"


def test_a_linestring_incident_takes_its_first_vertex():
    """Incidents arrive as points or as stretches of road, and the shape is not
    knowable in advance. Assuming one produced an index error on the other."""
    parsed = TomTomIncidentFeed._parse(
        {
            "geometry": {"type": "LineString", "coordinates": [[-97.29, 32.96], [-97.30, 32.97]]},
            "properties": {"magnitudeOfDelay": 2, "events": [{"description": "Roadworks"}]},
        },
        NOW,
    )
    assert (round(parsed.where.lat, 2), round(parsed.where.lon, 2)) == (32.96, -97.29)


def test_a_sparse_payload_does_not_raise():
    """A third party's schema is not ours to rely on. Missing fields become an
    incident with no effect rather than an exception mid-solve."""
    parsed = TomTomIncidentFeed._parse({}, NOW)
    assert parsed.severity == 0 and parsed.multiplier == 1.0


# ------------------------------------------------------- through the service


def test_a_live_incident_slows_the_matrix_the_same_way_an_event_does(tmp_path):
    """The whole point: no new path into the solver.

    A reported incident and a dispatcher's typed one end up as the same kind of object
    and are applied by the same provider wrapper.
    """
    from glass_guru.fixtures.sample_business import DEPOT, seed_events
    from glass_guru.persistence.log import Workspace
    from glass_guru.service import DispatchService

    workspace = Workspace(tmp_path / "ws")
    workspace.seed(seed_events())

    quiet = DispatchService(workspace, travel_mode="frozen", incident_feed=NoIncidentFeed())
    world = quiet.world()
    assert quiet.live_overrides(world) == ()

    busy = DispatchService(
        workspace,
        travel_mode="frozen",
        incident_feed=ScriptedIncidentFeed((incident(3, DEPOT),)),
    )
    overrides = busy.live_overrides(busy.world())
    assert len(overrides) == 2
    assert all(o.multiplier == 1.5 for o in overrides)


def test_a_feed_that_is_down_does_not_stop_a_booking(tmp_path):
    """A third party being unreachable must not fail a quote. Planning on last week's
    congestion is what the system did before this existed."""
    from glass_guru.fixtures.sample_business import seed_events
    from glass_guru.persistence.log import Workspace
    from glass_guru.service import DispatchService

    class Broken:
        def fetch(self, centre, radius_miles, now):
            raise ConnectionError("tomtom is down")

    workspace = Workspace(tmp_path / "ws")
    workspace.seed(seed_events())
    service = DispatchService(workspace, travel_mode="frozen", incident_feed=Broken())
    assert service.live_overrides(service.world()) == ()
