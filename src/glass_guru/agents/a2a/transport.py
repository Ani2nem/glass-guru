"""Delivering A2A tasks, over one transport or another.

The contract is defined once and the wire is a detail. Locally and in tests agents
run in one process and tasks are handed over directly; deployed, the same tasks go
over HTTP between separate services. Agents are written against the interface and
never learn which is in force.

That split is deliberate rather than lazy. Making every test spin up four HTTP
services would buy nothing and cost seconds on every run, and a suite people avoid
running is worse than one that exercises slightly less plumbing. What matters is that
the *protocol* - the task lifecycle, input-required, artifacts, agent cards - is real
in both, so nothing about moving to HTTP changes how an agent is written.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import httpx

from glass_guru.agents.a2a.types import AgentCard, Task, TaskState, TaskStatus
from glass_guru.obs.correlation import require_dispatch_id
from glass_guru.obs.tracing import record, span

#: An agent is anything that takes a task and returns one.
Handler = Callable[[Task], Task]


class AgentNotFound(LookupError):
    """No registered agent advertises what was asked for."""


@runtime_checkable
class A2ATransport(Protocol):
    def send(self, agent: str, task: Task) -> Task: ...
    def card(self, agent: str) -> AgentCard: ...
    def discover(self, skill_id: str) -> str: ...


class InProcessTransport:
    """Direct delivery. The default locally, in tests, and in evals."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._cards: dict[str, AgentCard] = {}

    def register(self, card: AgentCard, handler: Handler) -> None:
        self._handlers[card.name] = handler
        self._cards[card.name] = card

    def card(self, agent: str) -> AgentCard:
        if agent not in self._cards:
            raise AgentNotFound(f"no agent named {agent!r}; known: {sorted(self._cards)}")
        return self._cards[agent]

    def discover(self, skill_id: str) -> str:
        """Find an agent by what it can do rather than by name."""
        for name, card in sorted(self._cards.items()):
            if card.handles(skill_id):
                return name
        raise AgentNotFound(
            f"no agent advertises skill {skill_id!r}; "
            f"known skills: {sorted({s.id for c in self._cards.values() for s in c.skills})}"
        )

    def send(self, agent: str, task: Task) -> Task:
        if agent not in self._handlers:
            raise AgentNotFound(f"no agent named {agent!r}; known: {sorted(self._handlers)}")

        # The dispatch id travels with the task, so an A2A hop lands in the same trace
        # as the tool calls and solver runs either side of it.
        task.metadata.setdefault("dispatch_id", require_dispatch_id())
        with span("a2a.send", agent=agent, task_id=task.id) as active:
            try:
                result = self._handlers[agent](task)
            except Exception as exc:
                record(outcome="failed", error=str(exc))
                from glass_guru.agents.a2a.types import A2AMessage, Role

                return task.model_copy(
                    update={
                        "status": TaskStatus(
                            state=TaskState.FAILED,
                            message=A2AMessage.text(Role.AGENT, str(exc)),
                        )
                    }
                )
            active.set_attribute("state", result.state.value)
            record(outcome=result.state.value, artifacts=len(result.artifacts))
            return result


class HttpA2ATransport:
    """Delivery over HTTP to separately deployed agents.

    Agent Cards are fetched from the well-known path the protocol specifies, so a
    client needs only a base URL to learn what an agent can do.
    """

    WELL_KNOWN = "/.well-known/agent-card.json"

    def __init__(self, endpoints: dict[str, str], *, timeout: float = 30.0) -> None:
        self._endpoints = dict(endpoints)
        self._client = httpx.Client(timeout=timeout)
        self._cards: dict[str, AgentCard] = {}

    def card(self, agent: str) -> AgentCard:
        if agent in self._cards:
            return self._cards[agent]
        base = self._endpoints.get(agent)
        if base is None:
            raise AgentNotFound(f"no endpoint for {agent!r}")
        response = self._client.get(base.rstrip("/") + self.WELL_KNOWN)
        response.raise_for_status()
        card = AgentCard.model_validate(response.json())
        self._cards[agent] = card
        return card

    def discover(self, skill_id: str) -> str:
        for agent in sorted(self._endpoints):
            if self.card(agent).handles(skill_id):
                return agent
        raise AgentNotFound(f"no deployed agent advertises skill {skill_id!r}")

    def send(self, agent: str, task: Task) -> Task:
        base = self._endpoints.get(agent)
        if base is None:
            raise AgentNotFound(f"no endpoint for {agent!r}")
        task.metadata.setdefault("dispatch_id", require_dispatch_id())

        with span("a2a.send", agent=agent, task_id=task.id, transport="http") as active:
            response = self._client.post(
                base.rstrip("/") + "/tasks/send",
                json=task.model_dump(mode="json"),
                # Propagated as a header too, so a service that never opens the body
                # still logs under the right correlation id.
                headers={"x-dispatch-id": str(task.metadata.get("dispatch_id", ""))},
            )
            response.raise_for_status()
            result = Task.model_validate(response.json())
            active.set_attribute("state", result.state.value)
            return result

    def close(self) -> None:
        self._client.close()
