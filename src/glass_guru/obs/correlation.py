"""The ``dispatch_id``: one identifier from a sentence to a committed plan.

A disruption touches the triage agent, an A2A hop, several MCP tool calls, two solver
runs and a database write. Reconstructing that afterwards from separate logs is the
kind of archaeology that stops happening under pressure, which is precisely when you
need it. One identifier, minted the moment raw text enters and carried through
everything, turns the whole episode into a single trace.

A context variable rather than a parameter threaded through every signature: the
scheduler and the domain stay ignorant of tracing, and any code that wants the current
id can ask for it. Context variables propagate correctly into async tasks, which
matters once agents run concurrently.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_dispatch_id: ContextVar[str | None] = ContextVar("glass_guru_dispatch_id", default=None)


def new_dispatch_id(prefix: str = "d") -> str:
    """Mint an id. Short enough to paste into a message, long enough not to collide."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def current_dispatch_id() -> str | None:
    """The id in force, or ``None`` outside any dispatch."""
    return _dispatch_id.get()


def require_dispatch_id() -> str:
    """The id in force, minting a detached one if there is none.

    Work that reaches here without an id is still worth tracing - it just will not
    join up with anything else, which is a signal in itself.
    """
    return current_dispatch_id() or new_dispatch_id("orphan")


@contextmanager
def dispatch(dispatch_id: str | None = None) -> Iterator[str]:
    """Run a block under a dispatch id, restoring the previous one afterwards.

    Nesting reuses the outer id by default, because a tool call made while handling a
    disruption is part of that disruption rather than a new one.
    """
    resolved = dispatch_id or current_dispatch_id() or new_dispatch_id()
    token = _dispatch_id.set(resolved)
    try:
        yield resolved
    finally:
        _dispatch_id.reset(token)
