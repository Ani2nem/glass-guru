"""HTTP surface.

The board is a thin client over these endpoints, so anything asserted here is
something a dispatcher would otherwise have to notice on screen. The regression at
the bottom is the one that matters most: it was found by clicking, not by reasoning.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from glass_guru.api.models import TriageView
from glass_guru.fixtures.sample_business import WEEK_START, seed_events
from glass_guru.persistence.log import Workspace


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    workspace = Workspace(tmp_path / "ws")
    workspace.seed(seed_events())
    monkeypatch.setenv("GLASS_GURU_WORKSPACE", str(tmp_path / "ws"))
    # Real road distances from the committed snapshot: offline and identical to what a
    # deployment would compute.
    monkeypatch.setenv("GLASS_GURU_TRAVEL", "frozen")

    from glass_guru.api.main import app

    return TestClient(app)


#: The fixture's jobs live in a fixed week, and the horizon now starts from whatever
#: today is - correctly, since a board showing last week is the bug this fixed. Tests
#: that want the fixture's work have to say which week they mean.
COMMIT = f"/api/plan/commit?start_date={WEEK_START.isoformat()}"


def scheduled(plan: dict[str, Any]) -> set[str]:
    return {stop["job_id"] for route in plan["routes"] for stop in route["stops"]}


# ---------------------------------------------------------------------- reading


def test_world_reports_the_roster(client: TestClient):
    world = client.get("/api/world").json()
    assert len(world["workers"]) == 6
    assert len(world["vans"]) == 4
    assert len(world["jobs"]) == 10


def test_the_board_is_told_the_costs_are_uncalibrated(client: TestClient):
    """Every figure on screen rests on numbers nobody has validated. That belongs in
    front of the reader, not in a config file they will never open."""
    assert "estimate" in client.get("/api/world").json()["calibration_warning"]


def test_no_plan_yet_is_null_not_an_error(client: TestClient):
    response = client.get("/api/plan")
    assert response.status_code == 200
    assert response.json() is None


def test_params_carry_their_provenance(client: TestClient):
    params = client.get("/api/params").json()
    assert params
    assert all(p["source"] and p["note"] for p in params)


# --------------------------------------------------------------------- planning


def test_committing_returns_a_feasible_plan(client: TestClient):
    plan = client.post(COMMIT).json()
    assert plan["feasible"] and plan["violations"] == []
    assert len(scheduled(plan)) == 10


def test_the_board_gets_what_it_needs_to_draw_a_bar(client: TestClient):
    """Minutes from midnight rather than timestamps, so the client positions bars
    without parsing dates or guessing a timezone."""
    plan = client.post(COMMIT).json()
    stop = plan["routes"][0]["stops"][0]
    assert 0 <= stop["start_minute"] < stop["end_minute"] <= 24 * 60
    assert stop["lat"] and stop["lon"]
    assert stop["commitment_state"]


def test_routes_carry_utilisation_and_slack(client: TestClient):
    """The two columns that make a technically valid but obviously wrong plan look
    wrong. Neither is something an invariant check can judge."""
    plan = client.post(COMMIT).json()
    route = plan["routes"][0]
    assert 0.0 <= route["utilization"] <= 1.0
    assert route["idle_minutes"] >= 0


def test_unserved_separates_failures_from_routine(client: TestClient):
    plan = client.post(COMMIT).json()
    for item in plan["unserved"]:
        assert isinstance(item["is_failure"], bool)
        assert item["detail"]


# ----------------------------------------------------------------------- events


def test_recording_an_event_changes_the_world(client: TestClient):
    client.post(
        "/api/events",
        json={"kind": "van-unavailable", "target": "van-1", "at": "10:40"},
    )
    vans = {v["id"]: v for v in client.get("/api/world").json()["vans"]}
    assert vans["van-1"]["available"] is False


def test_an_incomplete_event_is_refused_with_a_remedy(client: TestClient):
    response = client.post("/api/events", json={"kind": "job-overran", "target": "j-403"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "--minutes" in detail["detail"]
    assert detail["remedy"]


def test_an_unknown_event_kind_is_refused(client: TestClient):
    assert client.post("/api/events", json={"kind": "abduction"}).status_code == 400


# ----------------------------------------------------------------------- repair


def test_repairing_without_a_plan_says_what_to_do(client: TestClient):
    response = client.post("/api/repair")
    assert response.status_code == 409
    assert "commit" in response.json()["detail"]["remedy"]


def test_repair_offers_priced_options_with_autonomy_verdicts(client: TestClient):
    client.post(COMMIT)
    client.post("/api/events", json={"kind": "van-unavailable", "target": "van-1", "at": "10:40"})

    repair = client.post("/api/repair").json()
    assert repair["candidates"]
    assert repair["recommended"]
    for candidate in repair["candidates"]:
        assert candidate["autonomy"] in {"auto_apply", "escalate"}
        assert candidate["blast_radius"] in {"internal", "crew_only", "customer_visible"}


def test_applying_an_unknown_strategy_is_a_404(client: TestClient):
    client.post(COMMIT)
    assert client.post("/api/repair/apply", params={"strategy": "wing_it"}).status_code == 404


def test_applying_a_repair_moves_the_head(client: TestClient):
    first = client.post(COMMIT).json()
    client.post("/api/events", json={"kind": "van-unavailable", "target": "van-1", "at": "10:40"})
    repair = client.post("/api/repair").json()
    applied = client.post("/api/repair/apply", params={"strategy": repair["recommended"]}).json()

    assert applied["plan_id"] != first["plan_id"]
    newest = next(h["plan_id"] for h in client.get("/api/history").json())
    assert newest == applied["plan_id"]


def test_a_customer_visible_repair_cannot_be_applied_without_approval(
    client: TestClient, monkeypatch
):
    """`force` is what a dispatcher's approval looks like over HTTP. The endpoint
    cannot be talked past, only overridden by a person who saw the diff."""
    from glass_guru.domain import autonomy as autonomy_module

    client.post(COMMIT)
    client.post("/api/events", json={"kind": "van-unavailable", "target": "van-1", "at": "10:40"})
    repair = client.post("/api/repair").json()

    forced = autonomy_module.AutonomyDecision(
        autonomy_module.Decision.ESCALATE, ("a customer would need telling",)
    )
    monkeypatch.setattr("glass_guru.service.decide", lambda *a, **k: forced)

    response = client.post("/api/repair/apply", params={"strategy": repair["recommended"]})
    assert response.status_code == 412
    assert "dispatcher" in response.json()["detail"]["remedy"]

    ok = client.post("/api/repair/apply", params={"strategy": repair["recommended"], "force": True})
    assert ok.status_code == 200


# -------------------------------------------------------------------- regression


def test_replanning_does_not_erase_work_already_in_flight(client: TestClient):
    """Found by clicking, not by reasoning.

    A job that has been dispatched is no longer "schedulable", so planning the week
    from scratch dropped it silently - a dispatcher pressing Re-plan at eleven would
    have erased the crew that left at six. Repair already carried in-flight work
    forward; a plain re-plan did not.
    """
    first = client.post(COMMIT).json()
    assert "j-401" in scheduled(first)

    client.post("/api/events", json={"kind": "job-dispatched", "target": "j-401", "at": "06:05"})

    again = client.post(COMMIT).json()
    assert "j-401" in scheduled(again), "dispatched work vanished from the re-planned board"
    assert len(scheduled(again)) == 10
    assert again["feasible"]


def test_every_commitment_state_can_reach_the_board(client: TestClient):
    """Each state is a different colour on the Gantt, so a state that never arrives is
    a colour nobody has ever seen."""
    client.post(COMMIT)
    client.post("/api/events", json={"kind": "job-dispatched", "target": "j-401", "at": "06:05"})
    client.post(
        "/api/events",
        json={
            "kind": "job-confirmed",
            "target": "j-402",
            "window_start": "09:00",
            "window_end": "15:00",
            "commitment_cost": 250,
        },
    )
    plan = client.post(COMMIT).json()
    states = {stop["commitment_state"] for r in plan["routes"] for stop in r["stops"]}
    assert {"provisional", "confirmed", "dispatched"} <= states


# ---------------------------------------------------------------- health probes


def test_liveness_does_no_work(client: TestClient):
    """A probe that touched the solver would restart healthy tasks mid-solve."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_each_thing_the_image_could_have_failed_to_ship(client: TestClient):
    """The three checks stand for three things the image copies selectively."""
    body = client.get("/api/ready").json()
    assert set(body["checks"]) == {"params", "travel", "board", "auth"}
    assert body["checks"]["params"] == "42 parameters"
    assert body["checks"]["travel"].startswith("frozen:")


def test_readiness_is_ready_when_the_board_is_there(client: TestClient, monkeypatch, tmp_path):
    """Asserted against a directory this test creates.

    An earlier version asserted `ready` against whatever happened to be on disk, which
    passed locally - where the board had been built - and failed in CI, where the job
    that runs the tests has no reason to build it. The check was right and the test was
    reading the developer's working tree.
    """
    from glass_guru.api import main

    monkeypatch.setattr(main, "WEB_DIST", tmp_path)
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_is_degraded_without_the_built_board(client: TestClient, monkeypatch, tmp_path):
    """An image that shipped no board serves a blank page and answers 200 on every API
    route. The load balancer should not call that a healthy target."""
    from glass_guru.api import main

    monkeypatch.setattr(main, "WEB_DIST", tmp_path / "never-built")
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["board"].startswith("FAILED")


def test_readiness_fails_loudly_when_travel_cannot_answer(client: TestClient, monkeypatch):
    """The failure worth catching: the process is up and cannot do the job.

    A port check calls this container healthy. It is not - a task wired to a routing
    backend that is not there would serve errors for every solve while the load
    balancer kept sending it traffic.
    """
    monkeypatch.setenv("GLASS_GURU_TRAVEL", "osrm")
    monkeypatch.setenv("GLASS_GURU_OSRM_URL", "http://127.0.0.1:1")

    response = client.get("/api/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["travel"].startswith("FAILED")
    assert body["checks"]["params"] == "42 parameters", "unrelated checks still report"


def test_the_readiness_probe_asks_for_a_leg_the_snapshot_holds():
    """Regression: the probe used hand-typed coordinates and a wall-clock timestamp.

    Both were wrong in a way that only showed up sometimes. The addresses are geocoded,
    so a literal copied from the source sat in a different geohash cell than anything
    frozen; and the snapshot is keyed by day type, so probing at "now" asked for a
    weekend leg every Saturday, and a healthy container reported degraded.

    Asserted against the snapshot directly rather than through the endpoint, because
    the endpoint answers correctly on six days out of seven either way.
    """
    from glass_guru.api.main import _PROBE_AT
    from glass_guru.config import BusinessParams
    from glass_guru.fixtures.sample_business import DEPOT, PROBE_STOP
    from glass_guru.scheduler.travel.factory import TravelMode, build_travel

    assert _PROBE_AT.weekday() < 5, "the business does not run weekends"

    frozen = build_travel(BusinessParams.load(), TravelMode.FROZEN)
    # Raises CacheMiss if this pair and bucket were never frozen, which is the bug.
    leg = frozen.leg(DEPOT, PROBE_STOP, _PROBE_AT)
    assert leg.minutes > 0


def test_the_osrm_url_is_configurable(monkeypatch):
    """Deployed, the routing backend is another host. localhost is a laptop default."""
    from glass_guru.config import BusinessParams
    from glass_guru.scheduler.travel.factory import TravelMode, build_travel

    monkeypatch.setenv("GLASS_GURU_OSRM_URL", "http://osrm.internal:5000")
    provider = build_travel(BusinessParams.load(), TravelMode.OSRM)
    assert "osrm.internal" in repr(provider.__dict__), "the env var was not honoured"


def test_the_board_is_told_how_to_watch_for_changes(client: TestClient, monkeypatch):
    """The client cannot work this out for itself.

    A stream works perfectly well on Lambda; it is just billed for every second it
    stays open, so there is no failure to detect and fall back from. The server has to
    say, and the default is the one that is right locally.
    """
    assert client.get("/api/health").json()["stream"] == "sse"

    monkeypatch.setenv("GLASS_GURU_STREAM", "poll")
    assert client.get("/api/health").json()["stream"] == "poll"


def test_streaming_is_refused_when_it_is_billed_by_the_second(client: TestClient, monkeypatch):
    """An old tab that kept its connection would go on costing money, and a bill is a
    bad way to find out. Refused with a remedy rather than quietly served."""
    monkeypatch.setenv("GLASS_GURU_STREAM", "poll")
    response = client.get("/api/stream")
    assert response.status_code == 409
    assert "poll" in response.json()["detail"]["remedy"]


def test_an_address_on_the_wrong_coast_is_refused_not_crashed(client: TestClient):
    """The bug a dispatcher actually hit, reported as a 500.

    "main st" typed during a call near Fort Worth resolves nationwide - a global
    geocoder ranks by prominence and has no idea where the vans are. The solver then
    asked the travel snapshot for a leg to New York, 2,403 miles away, and the cache
    miss surfaced as Internal Server Error.
    """
    from glass_guru.domain.models import Location
    from glass_guru.geocoding import OutsideServiceArea, for_service_area

    geocoder = for_service_area()
    manhattan = Location(lat=40.7589, lon=-73.9668, address="2nd Ave, Manhattan")
    with pytest.raises(OutsideServiceArea) as raised:
        geocoder._check_in_area("2nd ave", manhattan)
    # Far enough that no service radius could plausibly reach it. The exact figure
    # is a property of where the depot happens to be, so it is not asserted.
    assert raised.value.miles > 1000
    assert "service area" in str(raised.value)


def test_the_search_is_bounded_to_the_service_area():
    """A bias that merely prefers nearby results still returns the far one when nothing
    closer matches, which is exactly the failing case. It has to be a hard bound."""
    from glass_guru.geocoding import for_service_area

    box = for_service_area()._viewbox()
    assert box is not None
    west, north, east, south = (float(v) for v in box.split(","))
    assert west < -97.34 < east and south < 33.00 < north, "the depot is inside its own box"
    # A degree of longitude covers less ground than a degree of latitude this far
    # north - about 47 miles against 69 - so the box has to be wider than it is tall
    # by roughly 1/cos(47.6 degrees). Treating them as equal clips real addresses.
    expected = 1 / math.cos(math.radians(33.00))
    assert (east - west) / (north - south) == pytest.approx(expected, rel=0.02)


def test_a_new_address_explains_itself_rather_than_failing(client: TestClient):
    """The frozen snapshot refuses to invent a leg, which is right for tests and wrong
    mid-call. The answer is a remedy, not a stack trace."""
    from glass_guru.api.main import _travel_cache_miss
    from glass_guru.scheduler.travel.cache import CacheMiss

    # The handler ignores the request; typing it honestly beats a None the checker
    # has to be told to overlook.
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = _travel_cache_miss(request, CacheMiss("no cached leg for x|y|weekday|early"))
    assert response.status_code == 409
    assert b"warm" in response.body, "it must say which mode fixes it"


# ------------------------------------------------------------------ one box


def test_a_note_is_routed_to_the_right_agent(client: TestClient, monkeypatch):
    """The board used to ask the dispatcher which box to type into, and they got it
    wrong on the first try. The classifier answers instead."""
    from glass_guru.agents.router import NoteRouting

    monkeypatch.setattr(
        "glass_guru.agents.router.route_note",
        lambda *_a, **_k: type(
            "E", (), {"value": NoteRouting(kind="disruption", why="a van is off the road")}
        )(),
    )
    monkeypatch.setattr(
        "glass_guru.api.main.run_triage",
        lambda request: TriageView(state="ok", summary=request.text),
    )

    body = client.post("/api/note", json={"text": "van 3 won't start"}).json()
    assert body["kind"] == "disruption"
    assert body["why"] == "a van is off the road"
    assert body["booking"] is None, "only the agent that read it should answer"


def test_the_dispatcher_can_overrule_the_classifier(client: TestClient, monkeypatch):
    """What makes routing by model safe here.

    A misroute costs one click rather than a wrong job on the schedule, and the
    classifier is skipped entirely when the answer is already known - so an override
    cannot be silently re-overridden.
    """
    called = False

    def should_not_run(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("the classifier ran despite an explicit kind")

    monkeypatch.setattr("glass_guru.agents.router.route_note", should_not_run)
    monkeypatch.setattr(
        "glass_guru.api.main.run_triage", lambda request: TriageView(state="ok", summary="")
    )

    body = client.post("/api/note?kind=disruption", json={"text": "anything"}).json()
    assert body["kind"] == "disruption"
    assert body["why"] == "", "nothing was classified, so there is no reason to show"
    assert called is False


# ------------------------------------------------------------------------ auth


def test_without_a_key_configured_everything_is_open(client: TestClient):
    """What makes `make dev` work with no setup. Safe only because the deployment
    refuses to be public without one."""
    assert client.get("/api/world").status_code == 200


def test_with_a_key_configured_an_unauthenticated_call_is_refused(client: TestClient, monkeypatch):
    monkeypatch.setenv("GLASS_GURU_API_KEY", "s3cret")
    response = client.get("/api/world")
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["remedy"]


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "s3cret"},
        {"Authorization": "Bearer s3cret"},
        {"authorization": "bearer s3cret"},
    ],
)
def test_either_header_carries_the_key(client: TestClient, monkeypatch, headers: dict[str, str]):
    monkeypatch.setenv("GLASS_GURU_API_KEY", "s3cret")
    assert client.get("/api/world", headers=headers).status_code == 200


def test_a_wrong_key_is_refused(client: TestClient, monkeypatch):
    monkeypatch.setenv("GLASS_GURU_API_KEY", "s3cret")
    assert client.get("/api/world", headers={"X-API-Key": "s3cre"}).status_code == 401
    assert client.get("/api/world", headers={"X-API-Key": "s3cretx"}).status_code == 401


def test_health_and_readiness_stay_open(client: TestClient, monkeypatch):
    """A load balancer and a deploy smoke test decide whether this container works,
    and neither can hold a secret."""
    monkeypatch.setenv("GLASS_GURU_API_KEY", "s3cret")
    assert client.get("/api/health").status_code == 200
    # Not asserted as 200: readiness answers 503 wherever the board has not been
    # built, which is every CI run. The property here is that the key does not stand
    # in front of it, so what matters is that it is not a 401.
    assert client.get("/api/ready").status_code != 401


def test_readiness_says_whether_anything_is_guarding_the_door(client: TestClient, monkeypatch):
    """Running open is a legitimate local choice. Running open without knowing is not."""
    assert "open" in client.get("/api/ready").json()["checks"]["auth"]
    monkeypatch.setenv("GLASS_GURU_API_KEY", "s3cret")
    assert client.get("/api/ready").json()["checks"]["auth"] == "api key required"


# --------------------------------------------------------------- usable board


def test_a_fresh_workspace_has_no_work_in_it():
    """A business has staff and vans on day one and no jobs until somebody rings.
    Pre-booked work nobody booked is confusing on a board somebody is trying to use."""
    from glass_guru.domain.state import fold
    from glass_guru.fixtures.sample_business import seed_events

    empty = fold(seed_events(with_jobs=False))
    assert empty.workers and empty.vans
    assert empty.jobs == {}

    assert fold(seed_events()).jobs, "the fixture week is still there for scenarios"


def test_the_horizon_starts_from_the_next_working_day():
    """It used to return a date written into the fixture, so on any day but the week
    of 21 September 2026 the board showed a week that had already happened."""
    from datetime import date

    from glass_guru.api.main import next_working_day
    from glass_guru.domain.state import fold
    from glass_guru.fixtures.sample_business import seed_events

    world = fold(seed_events(with_jobs=False))
    saturday = date(2026, 9, 26)
    assert next_working_day(world, saturday) == date(2026, 9, 28)
    assert next_working_day(world, date(2026, 9, 28)) == date(2026, 9, 28)


def test_working_days_come_from_the_roster_not_from_an_assumption():
    """A business that starts opening Saturdays should not need a code change."""
    from datetime import date, time

    from glass_guru.api.main import next_working_day
    from glass_guru.domain.models import DayHours
    from glass_guru.domain.state import fold
    from glass_guru.fixtures.sample_business import seed_events

    world = fold(seed_events(with_jobs=False))
    only = next(iter(world.workers.values()))
    world.workers = {
        only.id: only.model_copy(
            update={"working_hours": (DayHours(weekday=5, start=time(9), end=time(13)),)}
        )
    }
    assert next_working_day(world, date(2026, 9, 26)).weekday() == 5


def test_a_quoted_window_opens_at_the_estimate_rather_than_straddling_it():
    """Centred, a 6:27 arrival was quoted as "5:27 to 7:27" - not a time anyone reads
    down a phone, and promising half an hour earlier than the crew could manage."""
    from datetime import datetime, timedelta

    from glass_guru.domain.models import TimeWindow

    arrival = datetime.fromisoformat("2026-09-28T06:27:00-05:00")
    opens = arrival.replace(minute=arrival.minute // 15 * 15, second=0, microsecond=0)
    window = TimeWindow(start=opens, end=opens + timedelta(minutes=120))

    assert window.start.hour == 6 and window.start.minute == 15
    assert window.start <= arrival, "never promise earlier than the crew can arrive"


def test_times_a_person_reads_are_twelve_hour():
    from datetime import time

    from glass_guru.formatting import clock, clock_range

    assert clock(time(8, 0)) == "8:00 AM"
    assert clock(time(17, 5)) == "5:05 PM"
    assert clock(time(0, 30)) == "12:30 AM"
    assert clock_range(time(8, 0), time(17, 0)) == "8:00 AM - 5:00 PM"
    # One meridiem when both sides agree; repeating it inside a range is noise.
    assert clock_range(time(9, 0), time(11, 30)) == "9:00 - 11:30 AM"


# ----------------------------------------------------------------- cancelling


def test_a_booking_can_be_taken_back_out(client: TestClient):
    """Customers change their minds on the same call that booked them. The event log
    keeps that the booking happened and then did not - undoing, not pretending."""
    client.post(COMMIT)
    world = client.get("/api/world").json()
    victim = world["jobs"][0]["id"]

    response = client.post(f"/api/jobs/{victim}/cancel").json()
    assert response["status"] == "freed"

    after = client.get("/api/world").json()
    assert victim not in {j["id"] for j in after["jobs"]}
    plan = client.get("/api/plan").json()
    assert victim not in {s["job_id"] for r in plan["routes"] for s in r["stops"]}, (
        "a cancelled job still on the board is a slot nobody will offer"
    )


def test_cancelling_a_job_that_is_not_there_says_so(client: TestClient):
    response = client.post("/api/jobs/j-nope/cancel")
    assert response.status_code == 404
    assert "reload" in response.json()["detail"]["remedy"]


def test_a_booking_cancelled_moments_later_stays_cancelled(client: TestClient):
    """The order of same-second events was decided by random event ids.

    New events were stamped with world.as_of - the fold clock, which is the *last
    event's* time - so a booking and its cancellation carried identical timestamps a
    week in the past, and the sort tiebreak (the random event id) decided whether the
    confirmation folded after the cancellation and quietly resurrected it. Roughly a
    coin flip, live. Wall-clock stamps make the order the order it happened in.
    """
    draft = {
        "customer_name": "Maria",
        "phone": "9132934243",
        "address": "somewhere real",
        "service_type": "residential_window_replacement",
        "duration_minutes": 120,
        "duration_confidence": 60,
        "crew_size": 1,
        "certifications": ["residential_glazing"],
        "commitment_cost": 0,
        "lat": 32.99,
        "lon": -97.36,
    }
    booked = client.post(
        "/api/book", json={"draft": draft, "date": "2026-09-21", "arrival": "16:00"}
    ).json()
    assert booked["status"] in {"scheduled", "booked but not yet scheduled"}

    cancelled = client.post(f"/api/jobs/{booked['job_id']}/cancel").json()
    assert cancelled["status"] == "freed"

    world = client.get("/api/world").json()
    assert booked["job_id"] not in {j["id"] for j in world["jobs"]}, (
        "the confirmation must not fold after the cancellation and resurrect it"
    )


def test_the_transcript_survives_from_call_to_reschedule(client: TestClient):
    """A reschedule starts from what the caller originally said, not from a
    dispatcher's memory of it. The words ride on the job's provenance."""
    said = "Maria needs glass fixed at 4pm monday, took time off, 9132934243, 14400 Artisan Dr"
    draft = {
        "customer_name": "Maria",
        "phone": "9132934243",
        "address": "14400 Artisan Dr",
        "service_type": "residential_window_replacement",
        "duration_minutes": 120,
        "duration_confidence": 60,
        "crew_size": 1,
        "certifications": ["residential_glazing"],
        "commitment_cost": 250,
        "lat": 32.99,
        "lon": -97.36,
    }
    booked = client.post(
        "/api/book",
        json={"draft": draft, "date": "2026-09-21", "arrival": "16:00", "transcript": said},
    ).json()

    world = client.get("/api/world").json()
    stored = next(j for j in world["jobs"] if j["id"] == booked["job_id"])
    assert stored["transcript"] == said


def test_the_rota_shows_each_fitter_week_with_overtime_reach(client: TestClient):
    """ "Why does the machine keep choosing Dan" was unanswerable from the screen. The
    rota carries the answer: shift per day, and how late overtime may keep them."""
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    ken = next(w for w in world["workers"] if w["name"] == "Ken")
    marcus = next(w for w in world["workers"] if w["name"] == "Marcus")

    assert len(dan["days"]) == 5, "a working week, not a single line"
    assert all(d["day"] and d["date"] for d in dan["days"])

    working = next(d for d in dan["days"] if d["shift"] != "off")
    assert "5:00 PM" in working["shift"]
    assert "7:00 PM" in working["reach"], "overtime reach explains who can take evenings"

    # Everyone is overtime-eligible now - the owner's call - so Ken reaches seven like
    # Dan, and the evening residential work stops being a one-man bottleneck.
    ken_day = next(d for d in ken["days"] if d["shift"] != "off")
    assert "7:00 PM" in ken_day["reach"]

    marcus_day = next(d for d in marcus["days"] if d["shift"] != "off")
    assert "5:00 PM" in marcus_day["reach"], "an early shift caps at five even on overtime"


def test_marking_a_fitter_out_shows_in_their_week(client: TestClient):
    client.post("/api/events", json={"kind": "worker-unavailable", "target": "w-dan"})
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    assert dan["available"] is False
    client.post("/api/events", json={"kind": "worker-restored", "target": "w-dan"})
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    assert dan["available"] is True


def test_a_one_day_mark_out_touches_exactly_one_day(client: TestClient):
    """Clicking a rota cell means "out THAT day". The first shipped version recorded
    "out from this moment", which at the wrong time of night either emptied the whole
    visible week or did nothing at all - both reported from the same evening."""
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    target = next(d["date"] for d in dan["days"][1:] if d["shift"] != "off")

    client.post(
        "/api/events",
        json={
            "kind": "worker-unavailable",
            "target": "w-dan",
            "window_start": f"{target}T00:00",
            "until": f"{target}T23:59",
        },
    )
    dan = next(w for w in client.get("/api/world").json()["workers"] if w["name"] == "Dan")
    out = [d["date"] for d in dan["days"] if d["shift"] != "off" and not d["available"]]
    assert out == [target], "exactly the clicked day, nothing else"
    assert dan["available"] is True, "a future absence does not grey the row today"

    # Clicking the out cell again brings that day back.
    client.post(
        "/api/events",
        json={
            "kind": "worker-restored",
            "target": "w-dan",
            "window_start": f"{target}T00:00",
            "window_end": f"{target}T23:59",
        },
    )
    dan = next(w for w in client.get("/api/world").json()["workers"] if w["name"] == "Dan")
    assert all(d["available"] for d in dan["days"] if d["shift"] != "off")


def test_restore_beats_an_outage_with_an_explicit_until(client: TestClient):
    """The bug that made the restore arrow a lie: restore only closed OPEN-ENDED
    outages, so anything recorded with an until could never be brought back."""
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    # Today's cell depends on what time the suite runs (a shift already over cannot
    # be "taken out"), so the assertions stick to strictly future days.
    future = [d["date"] for d in dan["days"][1:] if d["shift"] != "off"]

    client.post(
        "/api/events",
        json={"kind": "worker-unavailable", "target": "w-dan", "until": f"{future[-1]}T23:59"},
    )
    dan = next(w for w in client.get("/api/world").json()["workers"] if w["name"] == "Dan")
    assert not any(d["available"] for d in dan["days"] if d["date"] in future)

    client.post("/api/events", json={"kind": "worker-restored", "target": "w-dan"})
    dan = next(w for w in client.get("/api/world").json()["workers"] if w["name"] == "Dan")
    assert all(d["available"] for d in dan["days"] if d["date"] in future)


def test_restoring_one_day_carves_a_hole_in_a_longer_absence(client: TestClient):
    """ "He can do Wednesday after all" must not cancel the rest of the sick week."""
    world = client.get("/api/world").json()
    dan = next(w for w in world["workers"] if w["name"] == "Dan")
    week = [d["date"] for d in dan["days"][1:] if d["shift"] != "off"]
    assert len(week) >= 3
    middle = week[1]

    client.post(
        "/api/events",
        json={"kind": "worker-unavailable", "target": "w-dan", "until": f"{week[-1]}T23:59"},
    )
    client.post(
        "/api/events",
        json={
            "kind": "worker-restored",
            "target": "w-dan",
            "window_start": f"{middle}T00:00",
            "window_end": f"{middle}T23:59",
        },
    )
    dan = next(w for w in client.get("/api/world").json()["workers"] if w["name"] == "Dan")
    state_by_day = {d["date"]: d["available"] for d in dan["days"] if d["date"] in week}
    assert state_by_day[middle] is True, "the restored day is back"
    others = [v for k, v in state_by_day.items() if k != middle]
    assert not any(others), "every other day of the absence still stands"


def test_days_already_behind_the_clock_are_marked_unactionable(client: TestClient):
    """The board greys a day nothing can be done about instead of offering a click
    that silently does nothing. Strictly future days are always actionable; today
    depends on the hour the suite runs, so only its presence is asserted."""
    world = client.get("/api/world").json()
    for worker in world["workers"]:
        for d in worker["days"][1:]:
            assert d["actionable"] is True, f"{worker['name']} {d['date']} should be open"
        assert isinstance(worker["days"][0]["actionable"], bool)
