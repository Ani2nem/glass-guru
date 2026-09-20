"""Getting a validated object out of a language model, or failing loudly.

Nothing downstream ever sees an unvalidated model output. The loop is: ask, parse,
validate against a Pydantic schema, and on failure ask again with the *specific*
validation errors attached. After a bounded number of attempts it gives up and
escalates to a human rather than passing along something that merely looks right.

Three details carry most of the weight.

**The repair prompt names the actual errors.** "That was not valid, try again" gives a
small model nothing to work with; "field `crew_size`: input should be less than or
equal to 2, you sent 4" usually gets a correct answer on the next attempt.

**Attempts are bounded and escalation is a real outcome.** A model stuck on one wrong
answer will stay stuck, and a loop that keeps asking burns money to arrive nowhere.
Handing the dispatcher a half-filled form and a clear question is a better product
than a confident fabrication.

**The retry count is recorded.** It is the single most useful health signal for a
small model in production - a rising repair rate says the model is drifting from what
the prompt and schema expect, long before anyone notices a bad schedule.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from glass_guru.agents.llm.base import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMUsage,
    Message,
    Role,
)
from glass_guru.obs.tracing import record, span

DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Example:
    """A worked example. Few-shot matters much more for a small model than a large one."""

    text: str
    output: BaseModel


@dataclass(slots=True)
class Extraction[T: BaseModel]:
    """The result, and enough about how it was reached to judge it."""

    value: T | None
    attempts: int = 0
    errors: tuple[str, ...] = ()
    usage: LLMUsage = field(default_factory=LLMUsage)
    model_id: str = ""
    #: True when the model could not be reached at all, as opposed to answering badly.
    #: Very different problems: one is a configuration or outage issue for an operator,
    #: the other is a prompt or schema issue. Reporting them identically sends people
    #: to debug the wrong thing.
    provider_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.value is not None

    @property
    def escalated(self) -> bool:
        """True when the model could not produce a valid object and a human is needed."""
        return self.value is None

    @property
    def repairs(self) -> int:
        """Attempts beyond the first. The health signal worth alerting on, so it must
        count what actually happened rather than the configured ceiling."""
        return max(0, self.attempts - 1)

    def require(self) -> T:
        if self.value is None:
            raise LLMError(
                f"no valid result after {self.attempts} attempt(s): {'; '.join(self.errors)}"
            )
        return self.value


def json_schema_for(model_type: type[BaseModel]) -> dict[str, Any]:
    """A self-contained JSON schema.

    ``$ref`` and ``$defs`` are inlined because constrained-decoding paths vary in how
    well they follow references, and a schema a provider cannot fully honour silently
    becomes a weaker constraint - exactly what must not happen with a small model.
    """
    schema = model_type.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                target = dict(defs.get(name, {}))
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return inline({**target, **extra})
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    resolved: dict[str, Any] = inline(schema)
    resolved.setdefault("additionalProperties", False)
    return resolved


def _describe(error: ValidationError) -> str:
    """Validation errors in the terms the model needs to fix them."""
    parts: list[str] = []
    for item in error.errors()[:6]:
        location = ".".join(str(p) for p in item["loc"]) or "(root)"
        got = item.get("input")
        parts.append(f"field `{location}`: {item['msg']} (you sent {got!r})")
    return "; ".join(parts)


def _repair_prompt(errors: str, schema_name: str) -> str:
    return (
        f"That response did not validate. Fix exactly these problems and call "
        f"`{schema_name}` again with a corrected object:\n{errors}\n"
        "Change only what is wrong; keep every other field as you had it."
    )


def extract[T: BaseModel](
    provider: LLMProvider,
    model_type: type[T],
    *,
    system: str,
    text: str,
    examples: Sequence[Example] = (),
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    schema_description: str = "",
) -> Extraction[T]:
    """Ask for a ``model_type``, repairing on validation failure, then escalate."""
    schema_name = f"emit_{model_type.__name__.lower()}"
    schema = json_schema_for(model_type)

    preamble = "\n\n".join(
        f"Example input:\n{example.text}\n\nExample output:\n"
        f"{example.output.model_dump_json(indent=None)}"
        for example in examples
    )
    system_prompt = f"{system}\n\n{preamble}".strip() if preamble else system

    messages: list[Message] = [Message(role=Role.USER, text=text)]
    errors: list[str] = []
    usage = LLMUsage()
    attempted = 0
    provider_failed = False

    with span("llm.extract", schema=model_type.__name__, model=provider.model_id) as active:
        for attempt in range(1, max_attempts + 1):
            attempted = attempt
            request = LLMRequest(
                system=system_prompt,
                messages=tuple(messages),
                schema=schema,
                schema_name=schema_name,
                schema_description=schema_description or f"Emit a {model_type.__name__}.",
            )
            try:
                response = provider.complete(request)
            except LLMError as exc:
                # The model could not be reached. Retrying will not help, and pretending
                # the full retry budget was spent would corrupt the repair-rate signal.
                errors.append(str(exc))
                provider_failed = True
                break

            usage = usage + response.usage
            payload = response.structured
            if payload is None and response.text:
                # A model that ignored the schema and answered in prose. Worth one
                # attempt at rescuing, because the content is often right.
                payload = _json_from_text(response.text)

            if payload is None:
                errors.append("no structured output returned")
                messages += [
                    Message(role=Role.ASSISTANT, text=response.text or "(no output)"),
                    Message(
                        role=Role.USER,
                        text=f"You must call `{schema_name}` with a JSON object.",
                    ),
                ]
                continue

            try:
                value = model_type.model_validate(payload)
            except ValidationError as exc:
                detail = _describe(exc)
                errors.append(detail)
                messages += [
                    Message(role=Role.ASSISTANT, text=json.dumps(payload)),
                    Message(role=Role.USER, text=_repair_prompt(detail, schema_name)),
                ]
                continue

            record(attempts=attempt, repairs=attempt - 1, escalated=False)
            active.set_attribute("outcome", "ok")
            return Extraction(
                value=value,
                attempts=attempt,
                errors=tuple(errors),
                usage=usage,
                model_id=provider.model_id,
            )

        record(
            attempts=attempted,
            repairs=max(0, attempted - 1),
            escalated=True,
            provider_failed=provider_failed,
        )
        active.set_attribute("outcome", "provider_failed" if provider_failed else "escalated")
        return Extraction(
            value=None,
            attempts=attempted,
            errors=tuple(errors),
            usage=usage,
            model_id=provider.model_id,
            provider_failed=provider_failed,
        )


def _json_from_text(text: str) -> dict[str, Any] | None:
    """Best effort at the object inside a prose reply, including fenced code blocks."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [line for line in candidate.splitlines() if not line.startswith("```")]
        candidate = "\n".join(lines).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
