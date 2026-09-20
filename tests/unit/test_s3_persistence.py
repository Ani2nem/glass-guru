"""Storage, on disk and on S3, held to the same behaviour.

Two backends is how a subtle difference becomes a production bug, so the shared
behaviour is written once and run against both. Where they genuinely differ - and they
do, in exactly one place - the S3 case is asserted on its own.

That place is concurrency. `JsonPlanStore.commit` reads the head, compares, then
writes; between the read and the write there is a window a second writer can land in,
and on one machine with one process it is never lost, so the race is invisible. Behind
a function URL it is a matter of traffic. `S3PlanStore` closes it with a conditional
write, and the tests at the bottom are the ones that prove it, by interleaving two
writers deliberately.

moto is used rather than a hand-written fake precisely because it enforces the
preconditions. A fake that accepted every write would let all of this pass while
proving nothing.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from glass_guru.domain.models import CrewRoute, PlanVersion
from glass_guru.domain.state import fold
from glass_guru.fixtures.sample_business import WEEK_START, _at, seed_events
from glass_guru.persistence.log import PlanConflict, Workspace

BUCKET = "glass-guru-test"


@pytest.fixture
def aws():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield


@pytest.fixture(params=["disk", "s3"])
def workspace(request, tmp_path, aws, monkeypatch) -> Workspace:
    """The same workspace, backed two different ways."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if request.param == "disk":
        return Workspace(tmp_path / "ws")
    return Workspace(f"s3://{BUCKET}/business")


def plan(plan_id: str, parent: str | None = None) -> PlanVersion:
    return PlanVersion(
        id=plan_id,
        parent_id=parent,
        created_at=_at(0, 6),
        horizon_start=WEEK_START,
        horizon_end=WEEK_START,
    )


# ------------------------------------------------- the same on both backends


def test_an_empty_workspace_knows_it_is_empty(workspace: Workspace):
    assert not workspace.exists


def test_seeding_then_reading_round_trips(workspace: Workspace):
    """The regression that motivated `exists` being about events rather than files:
    derived values must not be stored, or they fail to load back."""
    workspace.seed(seed_events())
    assert workspace.exists

    world = fold(workspace.events.read())
    assert len(world.workers) == 6
    assert len(world.jobs) == 10


def test_seeding_twice_is_refused(workspace: Workspace):
    workspace.seed(seed_events())
    with pytest.raises(FileExistsError):
        workspace.seed(seed_events())


def test_appending_accumulates_rather_than_replaces(workspace: Workspace):
    events = seed_events()
    workspace.events.append(events[:3])
    workspace.events.append(events[3:6])
    assert len(workspace.events) == 6


def test_reading_as_of_a_moment_excludes_later_events(workspace: Workspace):
    events = seed_events()
    workspace.events.append(events)
    cutoff = events[2].recorded_at
    assert all(e.recorded_at <= cutoff for e in workspace.events.read(as_of=cutoff))


def test_a_committed_head_survives_a_restart(workspace: Workspace, tmp_path):
    workspace.seed(seed_events())
    workspace.plans.commit(plan("v1"), expected_parent=None)

    reopened = Workspace(workspace.root)
    head = reopened.plans.head()
    assert head is not None and head.id == "v1"


def test_a_stale_commit_is_rejected(workspace: Workspace):
    workspace.seed(seed_events())
    workspace.plans.commit(plan("v1"), expected_parent=None)
    with pytest.raises(PlanConflict):
        workspace.plans.commit(plan("v2"), expected_parent=None)


def test_history_and_ancestry_walk_the_chain(workspace: Workspace):
    workspace.seed(seed_events())
    workspace.plans.commit(plan("v1"), expected_parent=None)
    workspace.plans.commit(plan("v2", parent="v1"), expected_parent="v1")

    assert [p.id for p in workspace.plans.history()] == ["v1", "v2"]
    assert [p.id for p in workspace.plans.ancestry()] == ["v2", "v1"]


# ------------------------------------------------------------- S3 only


def test_the_head_moves_by_compare_and_swap(aws, monkeypatch):
    """Two writers that both solved against the same head. One must lose.

    The filesystem store would let both through if their reads interleaved, because
    the check and the write are separate operations. Here they are one.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = Workspace(f"s3://{BUCKET}/business").plans
    store.commit(plan("v1"), expected_parent=None)

    # Both read the head as v1, both solve, both try to commit against it.
    store.commit(plan("v2", parent="v1"), expected_parent="v1")
    with pytest.raises(PlanConflict) as raised:
        store.commit(plan("v3", parent="v1"), expected_parent="v1")

    assert raised.value.expected == "v1"
    head = store.head()
    assert head is not None and head.id == "v2", "the loser must not have moved the head"


def test_a_stored_version_cannot_be_rewritten(aws, monkeypatch):
    """Immutability enforced by the storage, not by the code remembering to."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = Workspace(f"s3://{BUCKET}/business").plans
    store.commit(plan("v1"), expected_parent=None)

    # Genuinely different work wearing an id that is already taken. It has to differ in
    # *content*: content_hash covers the scheduling decisions and deliberately ignores
    # metadata, so a plan that merely carries a later timestamp is the same plan.
    impostor = PlanVersion(
        id="v1",
        created_at=_at(0, 9),
        horizon_start=WEEK_START,
        horizon_end=WEEK_START,
        routes=(
            CrewRoute(crew_id="crew-van-1", date=WEEK_START, worker_ids=("w-ken",), van_id="van-1"),
        ),
    )
    assert impostor.content_hash != plan("v1").content_hash, "the test itself must differ"

    with pytest.raises(PlanConflict):
        store.commit(impostor, expected_parent="v1")

    stored = store.get("v1")
    assert stored is not None and stored.routes == (), "the original stands"


def test_an_identical_retry_is_allowed_to_finish(aws, monkeypatch):
    """The other half of the same rule, and the reason it is not a blanket refusal.

    Commit is two writes: the version object, then the head. A client whose first
    write landed and whose second did not - a timeout, a dropped connection - is left
    with the version stored and the head unmoved, and must be able to retry. A strict
    "never write an existing id" would strand it there forever.

    Identical content is what makes the retry safe to distinguish from an impostor,
    which is exactly what content_hash is for. The stranded state is built here rather
    than hoped for.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = Workspace(f"s3://{BUCKET}/business").plans
    store.commit(plan("v1"), expected_parent=None)

    # The version write landed; the head move did not.
    stranded = plan("v2", parent="v1")
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET,
        Key="business/plans/v2.json",
        Body=(stranded.model_dump_json(indent=1) + "\n").encode(),
    )
    head = store.head()
    assert head is not None and head.id == "v1", "the head really is unmoved"

    store.commit(stranded, expected_parent="v1")

    finished = store.head()
    assert finished is not None and finished.id == "v2", "the retry completed the commit"


def test_two_appends_from_the_same_read_conflict(aws, monkeypatch):
    """An event must never be silently dropped, which read-modify-write invites.

    Both writers load the log and build a new body from what they read. The second
    one's write is refused because the object moved underneath it, so a caller that
    retries gets both events and one that does not at least knows it failed.

    The interleaving is forced rather than hoped for: `second` reads, `first` writes,
    and only then does `second` try.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from glass_guru.persistence.s3 import S3EventLog

    events = seed_events()
    key = "business/events.jsonl"
    first = S3EventLog(BUCKET, key)
    second = S3EventLog(BUCKET, key)
    first.append(events[:2])

    def read_then_let_the_other_writer_in():
        state = S3EventLog._load(second)
        first.append(events[2:4])
        return state

    monkeypatch.setattr(second, "_load", read_then_let_the_other_writer_in)

    with pytest.raises(PlanConflict):
        second.append(events[4:5])

    assert len(first.read()) == 4, "the racing writer's events are intact"
