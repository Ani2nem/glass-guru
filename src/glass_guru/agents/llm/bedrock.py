"""Amazon Nova Lite (and anything else on Bedrock) via the Converse API.

Two choices worth explaining.

**Structured output comes from a forced tool call, not from asking for JSON.** The
request declares one tool whose input schema is the shape we want and sets
``toolChoice`` to that tool by name, so the model has no way to return prose, a fenced
code block, or a cheerful preamble to strip. For a small model this is the difference
between an extraction layer that mostly works and one that does. Parsing JSON out of
prose is a retry loop waiting to happen.

The ``strict`` flag on a tool specification is a further guarantee - the model is
constrained to emit a conforming object rather than merely asked to - but it is
**off by default**, because Nova Lite rejects it outright::

    ValidationException: This model doesn't support the strict field.

The field is in the Converse API shape; supporting it is a per-model matter, and
availability in the shape is not support by the model. That is a distinction only a
live call reveals, and it is why the validate-and-repair loop in
:mod:`glass_guru.agents.structured` carries real weight rather than being belt and
braces: without ``strict``, a malformed answer is a thing that happens, and the repair
prompt is what turns it into a correct one.

**Credentials come from the ambient AWS chain.** Nothing here takes an API key, so
deployment is an IAM task-role policy rather than a secret to distribute and rotate.

This code has been written against the Converse service model but not yet executed
against a live endpoint - see the note in the README. The shapes below were read from
botocore's own model rather than recalled.
"""

from __future__ import annotations

import json
import time
from typing import Any

from glass_guru.agents.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    Role,
)

#: Nova Lite: fast and cheap, and materially weaker at open-ended reasoning than a
#: frontier model. The agents are decomposed and constrained on that assumption.
DEFAULT_MODEL_ID = "us.amazon.nova-lite-v1:0"
DEFAULT_REGION = "us-east-1"

#: Model families known to accept ``strict`` on a tool specification. Deliberately an
#: allow-list rather than a deny-list: an unknown model gets the conservative request
#: that works everywhere, and the repair loop handles what strictness would have
#: prevented. Guessing the other way turns a new model id into a hard failure on the
#: first call.
STRICT_TOOL_MODELS: tuple[str, ...] = ("anthropic.claude", "us.anthropic.claude")


def supports_strict_tools(model_id: str) -> bool:
    return any(model_id.startswith(prefix) for prefix in STRICT_TOOL_MODELS)


class BedrockLLMProvider:
    """Converse-API provider."""

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        region: str = DEFAULT_REGION,
        *,
        client: Any | None = None,
        strict_tools: bool | None = None,
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._client = client
        self._strict_tools = (
            supports_strict_tools(model_id) if strict_tools is None else strict_tools
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    def _bedrock(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - boto3 is a dependency
                raise LLMError("boto3 is required to reach Bedrock") from exc
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": [
                {"role": m.role.value, "content": [{"text": m.text}]} for m in request.messages
            ],
            "inferenceConfig": {
                "maxTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        if request.system:
            body["system"] = [{"text": request.system}]

        if request.schema is not None:
            # One tool, forced. The model cannot answer in any other shape.
            body["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": request.schema_name,
                            "description": request.schema_description
                            or "Return the extracted result.",
                            "inputSchema": {"json": request.schema},
                            # Only where the model accepts it. Nova Lite returns a
                            # ValidationException for the field rather than ignoring it.
                            **({"strict": True} if self._strict_tools else {}),
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": request.schema_name}},
            }

        started = time.monotonic()
        try:
            response = self._bedrock().converse(**body)
        except Exception as exc:
            raise LLMError(f"Bedrock Converse failed: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        return _parse(response, self._model_id, latency_ms, request.schema_name)


def _parse(
    response: dict[str, Any], model_id: str, latency_ms: int, schema_name: str
) -> LLMResponse:
    message = response.get("output", {}).get("message", {})
    blocks = message.get("content", []) or []

    text_parts: list[str] = []
    structured: dict[str, Any] | None = None
    for block in blocks:
        if "text" in block:
            text_parts.append(block["text"])
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == schema_name:
            payload = tool_use.get("input")
            # Some models hand back the object; others hand back a JSON string.
            structured = json.loads(payload) if isinstance(payload, str) else payload

    usage = response.get("usage", {}) or {}
    return LLMResponse(
        text="\n".join(text_parts).strip(),
        structured=structured,
        stop_reason=response.get("stopReason", ""),
        usage=LLMUsage(
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            latency_ms=latency_ms,
        ),
        model_id=model_id,
    )


def user(text: str) -> tuple[Any, ...]:
    """Convenience for a single-turn request."""
    from glass_guru.agents.llm.base import Message

    return (Message(role=Role.USER, text=text),)
