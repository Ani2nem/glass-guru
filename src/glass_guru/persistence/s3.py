"""The same event log and plan store, on S3.

Written so the application can run on Lambda, where there is no disk that survives an
invocation. The interfaces are unchanged - ``EventLog`` and ``PlanStore`` from
:mod:`glass_guru.persistence.log` - so nothing above this line knows which backend it
has, and the same test bodies are run against both.

The interesting part is concurrency, and it is the one place where this backend is
*better* than the filesystem one rather than merely equivalent.

``JsonPlanStore.commit`` reads the head, compares it to what the caller solved
against, and then writes. Between the read and the write there is a window, and a
second writer landing in it silently discards the first customer's booking. On one
machine with one process that window is never lost, so the race is invisible; behind
a Lambda function URL it is a matter of traffic.

S3 conditional writes close it properly. ``If-Match`` on the ETag read at solve time
makes moving the head an atomic compare-and-swap: if anybody moved it in between, S3
refuses the write and the caller gets the ``PlanConflict`` it already knows how to
retry. ``If-None-Match: *`` on a version object makes immutability a property the
storage enforces rather than a convention the code observes.

A deliberate limit, stated rather than discovered later: the event log is one object,
read whole and rewritten to append. That is correct and cheap for the thousands of
events a business like this produces in a year, and it is the wrong shape for
millions. The line to cross is when reading the log stops being instant, and the
answer then is one object per batch, or a table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from glass_guru.domain.events import Event
from glass_guru.domain.models import PlanVersion
from glass_guru.persistence.log import _EVENT_ADAPTER, PlanConflict

#: boto3 ships no type information and the stub package is not a dependency worth
#: adding for one module, so the client is deliberately untyped here. Every call it is
#: used for is covered by the tests below it.
S3Client = Any

#: S3 reports a failed precondition as one of these. 412 is the documented answer for
#: If-Match and If-None-Match; 409 comes back when two writers collide mid-flight and
#: means the same thing to us - somebody else got there first.
_PRECONDITION_FAILED = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})


def parse_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/some/prefix`` -> ``("bucket", "some/prefix")``."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3 uri: {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def _client(client: S3Client | None = None) -> S3Client:
    if client is not None:
        return client
    import boto3

    return boto3.client("s3")


def _is_precondition_failure(error: Exception) -> bool:
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    return code in _PRECONDITION_FAILED


def _is_missing(error: Exception) -> bool:
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    return code in {"NoSuchKey", "404"}


class S3EventLog:
    """The append-only log as a single object.

    Appending is a read-modify-write under ``If-Match``, so two writers appending at
    once produce a conflict rather than a lost event. That is the right trade at this
    size: the log is small, appends are rare, and losing one is unacceptable.
    """

    def __init__(self, bucket: str, key: str, client: S3Client | None = None) -> None:
        self.bucket = bucket
        self.key = key
        self._s3 = _client(client)

    # ------------------------------------------------------------------ reading

    def _load(self) -> tuple[list[Event], str | None]:
        """Every event, and the ETag they were read at. The ETag is the whole point."""
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as error:
            if _is_missing(error):
                return [], None
            raise
        body = response["Body"].read().decode()
        events = [_EVENT_ADAPTER.validate_json(line) for line in body.splitlines() if line.strip()]
        return events, response["ETag"]

    def read(self, as_of: datetime | None = None) -> list[Event]:
        events, _ = self._load()
        if as_of is None:
            return events
        return [event for event in events if event.recorded_at <= as_of]

    def __len__(self) -> int:
        return len(self.read())

    # ------------------------------------------------------------------ writing

    def append(self, events: Sequence[Event]) -> None:
        if not events:
            return
        existing, etag = self._load()
        lines = [_EVENT_ADAPTER.dump_json(event).decode() for event in [*existing, *events]]
        body = ("\n".join(lines) + "\n").encode()

        # If-None-Match: * for the first write, If-Match after. Either way the write
        # fails rather than clobbering something that arrived since the read.
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=body,
                ContentType="application/x-ndjson",
                **condition,
            )
        except Exception as error:
            if _is_precondition_failure(error):
                raise PlanConflict(
                    expected="the log as it was read",
                    actual="another writer appended first",
                ) from error
            raise


class S3PlanStore:
    """Immutable versions as objects, with a head pointer moved by compare-and-swap."""

    def __init__(self, bucket: str, prefix: str, client: S3Client | None = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self._s3 = _client(client)

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    @property
    def head_key(self) -> str:
        return self._key("HEAD")

    # ------------------------------------------------------------------ reading

    def _head_pointer(self) -> tuple[str | None, str | None]:
        """The id the head points at, and the ETag it was read at."""
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=self.head_key)
        except Exception as error:
            if _is_missing(error):
                return None, None
            raise
        return response["Body"].read().decode().strip(), response["ETag"]

    def head(self) -> PlanVersion | None:
        plan_id, _ = self._head_pointer()
        return self.get(plan_id) if plan_id else None

    def get(self, plan_id: str) -> PlanVersion | None:
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=self._key(f"{plan_id}.json"))
        except Exception as error:
            if _is_missing(error):
                return None
            raise
        return PlanVersion.model_validate_json(response["Body"].read().decode())

    def history(self) -> list[PlanVersion]:
        """Every stored version, oldest first."""
        plans: list[PlanVersion] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key("")):
            for item in page.get("Contents", []):
                if not item["Key"].endswith(".json"):
                    continue
                body = self._s3.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()
                plans.append(PlanVersion.model_validate_json(body.decode()))
        return sorted(plans, key=lambda plan: (plan.created_at, plan.id))

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

        Unlike the filesystem store, the check and the write are one operation. There
        is no window between reading the head and moving it for a second writer to land
        in, because S3 refuses the write if the ETag no longer matches.
        """
        current_id, etag = self._head_pointer()
        if current_id != expected_parent:
            raise PlanConflict(expected_parent, current_id)

        # Immutability enforced by storage rather than by convention: a version that
        # already exists cannot be rewritten, whatever the caller intends.
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self._key(f"{plan.id}.json"),
                Body=(plan.model_dump_json(indent=1) + "\n").encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except Exception as error:
            if not _is_precondition_failure(error):
                raise
            # Writing the same version twice is only a problem if the content differs;
            # an identical retry after a failed head move should be able to proceed.
            stored = self.get(plan.id)
            if stored is None or stored.content_hash != plan.content_hash:
                raise PlanConflict(expected_parent, current_id) from error

        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self.head_key,
                Body=plan.id.encode(),
                ContentType="text/plain",
                **condition,
            )
        except Exception as error:
            if _is_precondition_failure(error):
                # Somebody moved the head between the read above and here. The version
                # object is already stored and harmless - it is simply not the head.
                raise PlanConflict(expected_parent, self._head_pointer()[0]) from error
            raise
        return plan
