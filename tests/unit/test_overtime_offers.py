"""The volunteer-overtime flow: offered, first yes wins, fallback stands.

The owner's policy is that overtime is volunteered rather than assigned: the solver
proves the slot feasible and pencils somebody in, the crew gets first refusal on the
extra money, and the customer's promise never waits on the group chat.
"""

from __future__ import annotations

from krama.domain.events import OvertimeClaimed, OvertimeOffered
from krama.domain.state import fold
from krama.fixtures.sample_business import WEEK_START, _at, seed_events
from krama.notify import LogNotifier, RecordingNotifier, TwilioNotifier, build_notifier


def _offer(**overrides: object) -> OvertimeOffered:
    base: dict[str, object] = {
        "event_id": "e-offer",
        "occurred_at": _at(0, 18),
        "recorded_at": _at(0, 18),
        "dispatch_id": "t",
        "job_id": "j-402",
        "on_date": WEEK_START,
        "offered_to": ("w-dan", "w-ken"),
        "fallback": "w-dan",
        "overtime_minutes": 45,
        "claim_deadline": _at(0, 20),
    }
    base.update(overrides)
    return OvertimeOffered(**base)


def _claim(worker: str, event_id: str = "e-claim") -> OvertimeClaimed:
    return OvertimeClaimed(
        event_id=event_id,
        occurred_at=_at(0, 19),
        recorded_at=_at(0, 19),
        dispatch_id="t",
        job_id="j-402",
        worker_id=worker,
    )


def test_first_yes_wins_and_the_rest_are_noise():
    world = fold([*seed_events(), _offer(), _claim("w-ken"), _claim("w-dan", "e-claim2")])
    offer = world.overtime_offers["j-402"]
    assert offer.claimed_by == "w-ken", "the second yes changes nothing"


def test_a_claim_from_someone_never_offered_is_ignored():
    """Sofia is not residential-certified; her yes cannot put her on the job."""
    world = fold([*seed_events(), _offer(), _claim("w-sofia")])
    assert world.overtime_offers["j-402"].claimed_by is None


def test_status_tracks_the_clock():
    world = fold([*seed_events(), _offer()])
    offer = world.overtime_offers["j-402"]
    assert offer.status(_at(0, 19)) == "open"
    assert offer.status(_at(0, 21)) == "expired", "deadline passed, the fallback stands"
    claimed = fold([*seed_events(), _offer(), _claim("w-ken")]).overtime_offers["j-402"]
    assert claimed.status(_at(0, 21)) == "claimed"


def test_cancelling_the_job_withdraws_the_offer():
    from krama.domain.events import JobCancelled

    cancelled = JobCancelled(
        event_id="e-cx",
        occurred_at=_at(0, 19, 30),
        recorded_at=_at(0, 19, 30),
        dispatch_id="t",
        job_id="j-402",
    )
    world = fold([*seed_events(), _offer(), cancelled])
    assert "j-402" not in world.overtime_offers, "no claiming hours on a dead job"


def test_a_claim_pins_the_fitter_in_the_solver(monkeypatch):
    """Ken says yes to j-402's evening; the next solve puts Ken on j-402."""
    from krama.fixtures.sample_business import BUSINESS_TZ
    from krama.scheduler.day_planner import SolveParams, plan_day
    from krama.scheduler.travel.cache import CachingTravelProvider
    from krama.scheduler.travel.synthetic import SyntheticTravelProvider

    world = fold([*seed_events(), _offer(), _claim("w-ken")])
    travel = CachingTravelProvider(SyntheticTravelProvider())
    result = plan_day(
        world=world,
        travel=travel,
        on_date=WEEK_START,
        candidate_job_ids=[j.id for j in world.active_jobs()],
        params=SolveParams(business_tz=BUSINESS_TZ, max_solve_seconds=20.0),
    )
    crew = next((r.worker_ids for r in result.routes for s in r.stops if s.job_id == "j-402"), None)
    assert crew is not None and "w-ken" in crew, f"claimant must serve the job, got {crew}"


def test_the_pin_dissolves_when_the_claimant_is_marked_out():
    """A claim is not a suicide pact: Ken claims, then calls in sick - the job must
    still be served, by somebody else."""
    from krama.domain.events import WorkerUnavailable
    from krama.fixtures.sample_business import BUSINESS_TZ
    from krama.scheduler.day_planner import SolveParams, plan_day
    from krama.scheduler.travel.cache import CachingTravelProvider
    from krama.scheduler.travel.synthetic import SyntheticTravelProvider

    sick = WorkerUnavailable(
        event_id="e-sick",
        occurred_at=_at(0, 5),
        recorded_at=_at(0, 5),
        dispatch_id="t",
        worker_id="w-ken",
        from_time=_at(0, 0),
        until_time=_at(0, 23),
    )
    world = fold([*seed_events(), _offer(), _claim("w-ken"), sick])
    travel = CachingTravelProvider(SyntheticTravelProvider())
    result = plan_day(
        world=world,
        travel=travel,
        on_date=WEEK_START,
        candidate_job_ids=[j.id for j in world.active_jobs()],
        params=SolveParams(business_tz=BUSINESS_TZ, max_solve_seconds=20.0),
    )
    crew = next((r.worker_ids for r in result.routes for s in r.stops if s.job_id == "j-402"), None)
    assert crew is not None, "the job is served despite the dissolved pin"
    assert "w-ken" not in crew


# ------------------------------------------------------------------- notifier


def test_the_default_notifier_logs_and_never_networks():
    delivery = LogNotifier().send("+18175550143", "hello")
    assert delivery.accepted and "logged" in delivery.detail
    assert not LogNotifier().send("", "no number").accepted


def test_build_notifier_defaults_to_log_mode(monkeypatch):
    monkeypatch.delenv("KRAMA_SMS", raising=False)
    assert isinstance(build_notifier(), LogNotifier)


def test_twilio_mode_refuses_to_start_half_configured(monkeypatch):
    import pytest

    monkeypatch.setenv("KRAMA_SMS", "twilio")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID"):
        build_notifier()


def test_twilio_request_shape(monkeypatch):
    """No network: capture the request and check the envelope Twilio documents."""
    import urllib.request

    captured: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"sid": "SM123"}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    notifier = TwilioNotifier("AC00", "secret", "+15550000000")
    delivery = notifier.send("+18175550143", "shift runs late")
    assert delivery.accepted and delivery.detail == "SM123"
    assert "Accounts/AC00/Messages.json" in captured["url"]
    assert "To=%2B18175550143" in captured["body"]
    assert captured["auth"].startswith("Basic ")


def test_recording_notifier_for_tests():
    recorder = RecordingNotifier()
    recorder.send("+1", "a")
    assert recorder.sent == [("+1", "a")]
