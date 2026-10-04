"""The model boundary.

One interface, so which model runs is a configuration value rather than a code
dependency. That matters more here than usual: the plan is to run Amazon Nova Lite,
a small and cheap model, and the honest way to find out whether it is good enough is
to run the eval suite across several models and read the numbers. That is only
possible if swapping one costs nothing.

The interface is shaped around *structured extraction* rather than chat, because that
is what all four agents actually do. Free-form text is the exception, not the rule -
an agent that returns prose has produced something nothing downstream can check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call.

    When ``schema`` is set the provider must return JSON conforming to it, by whatever
    mechanism the model supports best. Asking a small model for JSON in prose and
    parsing what comes back is markedly less reliable than using a provider's own
    constrained-output path, so that choice is left to the provider rather than
    imposed here.
    """

    system: str
    messages: tuple[Message, ...]
    schema: dict[str, Any] | None = None
    schema_name: str = "result"
    schema_description: str = ""
    max_tokens: int = 2048
    #: Extraction wants the same answer every time, so the default is deterministic.
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str = ""
    structured: dict[str, Any] | None = None
    stop_reason: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    model_id: str = ""


class LLMError(RuntimeError):
    """The model could not be reached, or refused."""


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...
