"""Agent-to-agent message shapes, following the A2A protocol.

A2A and MCP solve different problems, and conflating them is the usual mistake. MCP is
how an agent reaches a *tool* - a typed function with a schema. A2A is how an agent
reaches another *agent* - something with its own judgement, its own latency, and the
right to come back and ask a question instead of answering.

That last part is why the task lifecycle exists rather than a request/response call.
``input-required`` is a first-class state here: triage reading "Dan says the van's
making a noise" should be able to stop and ask which van, and a plain function return
has nowhere to put that.

Agent Cards are the discovery mechanism: each agent publishes what it can do, and a
client picks by capability rather than by hard-coded address.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskState(StrEnum):
    """The A2A task lifecycle."""

    SUBMITTED = "submitted"
    WORKING = "working"
    #: The agent needs something from the caller before it can continue. The state
    #: that makes "ask rather than guess" expressible across an agent boundary.
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class Role(StrEnum):
    USER = "user"
    AGENT = "agent"


class TextPart(Strict):
    kind: str = "text"
    text: str


class DataPart(Strict):
    """Structured content. Where typed results travel between agents."""

    kind: str = "data"
    data: dict[str, Any]


Part = TextPart | DataPart


class A2AMessage(Strict):
    message_id: str = Field(default_factory=lambda: f"msg-{uuid4().hex[:12]}")
    role: Role
    parts: list[Part] = Field(default_factory=list)

    @classmethod
    def text(cls, role: Role, text: str) -> A2AMessage:
        return cls(role=role, parts=[TextPart(text=text)])

    @classmethod
    def data(cls, role: Role, data: dict[str, Any], text: str = "") -> A2AMessage:
        parts: list[Part] = [DataPart(data=data)]
        if text:
            parts.insert(0, TextPart(text=text))
        return cls(role=role, parts=parts)

    @property
    def text_content(self) -> str:
        return "\n".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def data_content(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for part in self.parts:
            if isinstance(part, DataPart):
                merged.update(part.data)
        return merged


class Artifact(Strict):
    """A durable output of a task, as opposed to conversational turns."""

    artifact_id: str = Field(default_factory=lambda: f"art-{uuid4().hex[:12]}")
    name: str = ""
    parts: list[Part] = Field(default_factory=list)

    @property
    def data(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for part in self.parts:
            if isinstance(part, DataPart):
                merged.update(part.data)
        return merged


class TaskStatus(Strict):
    state: TaskState
    message: A2AMessage | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Task(Strict):
    """One unit of work handed to an agent."""

    id: str = Field(default_factory=lambda: f"task-{uuid4().hex[:12]}")
    context_id: str = ""
    status: TaskStatus
    history: list[A2AMessage] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    #: The dispatch id, carried so an A2A hop joins the same trace as everything else.
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def state(self) -> TaskState:
        return self.status.state

    @property
    def done(self) -> bool:
        return self.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}

    @property
    def needs_input(self) -> bool:
        return self.state is TaskState.INPUT_REQUIRED

    def artifact(self, name: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.name == name), None)


class AgentSkill(Strict):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCapabilities(Strict):
    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = True


class AgentCard(Strict):
    """What an agent advertises about itself.

    Discovery by capability rather than by hard-coded address is what lets the
    coordinator be written against "something that can triage" instead of against a
    particular module.
    """

    name: str
    description: str
    version: str = "0.1.0"
    url: str = ""
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["application/json"])
    skills: list[AgentSkill] = Field(default_factory=list)

    def handles(self, skill_id: str) -> bool:
        return any(s.id == skill_id for s in self.skills)


def submitted(text: str, *, context_id: str = "", dispatch_id: str = "") -> Task:
    """A fresh task carrying a caller's message."""
    message = A2AMessage.text(Role.USER, text)
    return Task(
        context_id=context_id or f"ctx-{uuid4().hex[:12]}",
        status=TaskStatus(state=TaskState.SUBMITTED, message=message),
        history=[message],
        metadata={"dispatch_id": dispatch_id} if dispatch_id else {},
    )
