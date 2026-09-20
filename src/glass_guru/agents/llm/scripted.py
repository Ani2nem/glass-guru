"""A deterministic stand-in for a model.

Every agent test runs against this: no network, no credentials, no spend, and the
same answer every time. That is not a compromise - a suite that needed a live model
would be slow, flaky and expensive enough that people would stop running it, and an
agent layer nobody tests is an agent layer nobody trusts.

What makes it useful rather than merely fast is that it can be told to *misbehave*.
The interesting questions about a small model are not "does the happy path work" but
"what happens when it returns the wrong shape, or an invalid enum, or the same wrong
answer three times running". Those are the cases the validate-and-repair loop exists
for, and scripting them is the only way to test them reliably.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from glass_guru.agents.llm.base import LLMError, LLMRequest, LLMResponse, LLMUsage


@dataclass
class ScriptedLLMProvider:
    """Returns queued responses in order, recording every request it was given."""

    responses: list[LLMResponse | Exception] = field(default_factory=list)
    model_id_value: str = "scripted"
    requests: list[LLMRequest] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return self.model_id_value

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.responses:
            raise LLMError("scripted provider exhausted: more calls than responses queued")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    # ------------------------------------------------------------------ builders

    @classmethod
    def returning(cls, *payloads: dict[str, Any] | Exception) -> ScriptedLLMProvider:
        """Queue structured payloads, in order."""
        return cls(
            responses=[
                p
                if isinstance(p, Exception)
                else LLMResponse(
                    structured=p,
                    stop_reason="tool_use",
                    usage=LLMUsage(input_tokens=120, output_tokens=40, latency_ms=90),
                    model_id="scripted",
                )
                for p in payloads
            ]
        )

    @classmethod
    def always(cls, payload: dict[str, Any], times: int = 8) -> ScriptedLLMProvider:
        """A model stuck on one answer - the case that must end in escalation, not a loop."""
        return cls.returning(*([payload] * times))

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def last_user_text(self) -> str:
        if not self.requests:
            return ""
        messages = self.requests[-1].messages
        return messages[-1].text if messages else ""


@dataclass
class CallbackLLMProvider:
    """Computes a response from the request, for tests that need to react to it."""

    handler: Callable[[LLMRequest], LLMResponse]
    model_id_value: str = "callback"
    requests: list[LLMRequest] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return self.model_id_value

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self.handler(request)


def structured(payload: dict[str, Any]) -> LLMResponse:
    return LLMResponse(structured=payload, stop_reason="tool_use", model_id="scripted")


def prose(text: str) -> LLMResponse:
    """A model that ignored the schema and replied in prose. It happens."""
    return LLMResponse(text=text, stop_reason="end_turn", model_id="scripted")


def sequence(payloads: Sequence[dict[str, Any]]) -> ScriptedLLMProvider:
    return ScriptedLLMProvider.returning(*payloads)
