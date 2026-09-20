"""Durable event log and plan store.

The event log is append-only and is the only source of truth; world state is always
``fold(events, as_of)``. Two properties follow, and both matter more than the storage
technology underneath:

* Any moment is reconstructible. "What did we know at 10:52?" is answerable, which is
  what lets a disruption scenario replay byte-identically in a test.
* Nothing is ever mutated, so there is no state to corrupt - only a history to read.

Plans are stored as immutable versions with a parent pointer, and committing one
requires naming the version it was built from. Two dispatchers taking calls at the
same time will otherwise both solve against the same head and the second write will
silently discard the first customer's booking. Optimistic concurrency turns that into
a conflict the caller retries, and since insertion is sub-second the retry is
invisible.

JSONL and JSON-per-version are deliberately boring. The interfaces below are what the
rest of the system depends on; a Postgres adapter is a drop-in replacement and changes
nothing above this line.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from glass_guru.domain.events import Event
from glass_guru.domain.models import PlanVersion

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _is_s3(root: Path | str) -> bool:
    return isinstance(root, str) and root.startswith("s3://")


class PlanConflict(RuntimeError):
    """The plan head moved while this change was being prepared.

    Not an error condition so much as a normal outcome under concurrency: re-read the
    head, redo the work against it, and commit again.
    """

    def __init__(self, expected: str | None, actual: str | None) -> None:
        super().__init__(
            f"plan head is {actual!r}, not {expected!r}; "
            "re-solve against the current head and retry"
        )
        self.expected = expected
        self.actual = actual


class EventLog(Protocol):
    def append(self, events: Sequence[Event]) -> None: ...
    def read(self, as_of: datetime | None = None) -> list[Event]: ...
    def __len__(self) -> int: ...


class JsonlEventLog:
    """One JSON object per line. Appending is the only write, which is the point."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, events: Sequence[Event]) -> None:
        if not events:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            for event in events:
                handle.write(_EVENT_ADAPTER.dump_json(event).decode() + "\n")

    def read(self, as_of: datetime | None = None) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open() as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = _EVENT_ADAPTER.validate_json(line)
                if as_of is None or event.recorded_at <= as_of:
                    events.append(event)
        return events

    def __len__(self) -> int:
        return len(self.read())


class PlanStore(Protocol):
    def head(self) -> PlanVersion | None: ...
    def get(self, plan_id: str) -> PlanVersion | None: ...
    def commit(self, plan: PlanVersion, expected_parent: str | None) -> PlanVersion: ...
    def history(self) -> list[PlanVersion]: ...
    # Part of the contract, and omitted here until a second implementation made that
    # obvious: the CLI and the API both walk the parent chain, and they were reaching
    # through the protocol to a concrete class to do it.
    def ancestry(self, plan_id: str | None = None) -> list[PlanVersion]: ...


class JsonPlanStore:
    """Immutable versions on disk, with a head pointer written atomically."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.head_path = self.directory / "HEAD"

    # ------------------------------------------------------------------ reading

    def head(self) -> PlanVersion | None:
        if not self.head_path.exists():
            return None
        return self.get(self.head_path.read_text().strip())

    def get(self, plan_id: str) -> PlanVersion | None:
        path = self.directory / f"{plan_id}.json"
        if not path.exists():
            return None
        return PlanVersion.model_validate_json(path.read_text())

    def history(self) -> list[PlanVersion]:
        """Every stored version, oldest first."""
        plans = [
            PlanVersion.model_validate_json(path.read_text())
            for path in self.directory.glob("*.json")
        ]
        return sorted(plans, key=lambda p: (p.created_at, p.id))

    def ancestry(self, plan_id: str | None = None) -> list[PlanVersion]:
        """The chain from the given version back to the first, newest first."""
        current = self.get(plan_id) if plan_id else self.head()
        chain: list[PlanVersion] = []
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.get(current.parent_id) if current.parent_id else None
        return chain

    # ------------------------------------------------------------------ writing

    def commit(self, plan: PlanVersion, expected_parent: str | None) -> PlanVersion:
        """Store a version and move the head, provided the head has not moved.

        ``expected_parent`` is the version the caller solved against. If the head has
        advanced since, the work is stale and must be redone - the alternative is
        silently discarding whatever the other writer committed.
        """
        current = self.head()
        current_id = current.id if current else None
        if current_id != expected_parent:
            raise PlanConflict(expected_parent, current_id)

        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{plan.id}.json").write_text(plan.model_dump_json(indent=1) + "\n")
        _atomic_write(self.head_path, plan.id)
        return plan


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so a crash cannot leave a torn head."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, prefix=f".{path.name}."
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        staged = handle.name
    os.replace(staged, path)


class Workspace:
    """One business's log and plan history, on disk or on S3.

    The location decides the backend: an ``s3://bucket/prefix`` root gets the S3
    stores, anything else gets the filesystem ones. Everything above this class holds
    the ``EventLog`` and ``PlanStore`` protocols and cannot tell which it has, which is
    what lets the same tests run against both and what let the application move to
    Lambda without the scheduler noticing.
    """

    def __init__(self, root: Path | str = ".glass-guru") -> None:
        self.root = root if _is_s3(root) else Path(root)
        self.events: EventLog
        self.plans: PlanStore

        if _is_s3(root):
            from glass_guru.persistence.s3 import S3EventLog, S3PlanStore, parse_uri

            bucket, prefix = parse_uri(str(root))
            self.events = S3EventLog(bucket, f"{prefix}/events.jsonl".lstrip("/"))
            self.plans = S3PlanStore(bucket, f"{prefix}/plans".lstrip("/"))
        else:
            self.events = JsonlEventLog(Path(root) / "events.jsonl")
            self.plans = JsonPlanStore(Path(root) / "plans")

    @property
    def exists(self) -> bool:
        """Whether this workspace has been initialised.

        Asked as "are there any events", not "is there a file", so it means the same
        thing on both backends.
        """
        return len(self.events) > 0

    def seed(self, events: Iterable[Event]) -> int:
        """Initialise an empty workspace. Refuses to overwrite an existing one."""
        if self.exists:
            raise FileExistsError(
                f"{self.root} already holds an event log; delete it to start over"
            )
        batch = list(events)
        self.events.append(batch)
        return len(batch)

    def describe(self) -> str:
        head = self.plans.head()
        return (
            f"{self.root}: {len(self.events)} event(s), "
            f"{len(self.plans.history())} plan version(s), "
            f"head {head.id if head else '(none)'}"
        )
