"""Tracing, wired before the first agent call rather than after the first outage.

Tracing is a development tool before it is a production one. With a small model
producing structured output, the question "what exactly did it see, and what did it
return" comes up constantly, and an answer that requires adding instrumentation first
arrives too late to be useful.

Two backends, one API:

* **OpenTelemetry** covers the deterministic path - MCP tool calls, solver runs,
  plan commits. Console exporter locally, CloudWatch once deployed.
* **LangSmith** covers LLM and agent runs, where its notion of inputs, outputs and
  feedback is worth more than a generic span. LangGraph instruments itself; this
  module only has to configure the client and carry the dispatch id across.

Both are optional. With neither configured every helper here is a cheap no-op, which
is what keeps the test suite offline and free - an observability layer that made tests
require network would simply be switched off.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from glass_guru.obs.correlation import require_dispatch_id

SERVICE_NAME = "glass-guru"
_configured = False


def configure(
    *,
    service: str = SERVICE_NAME,
    console: bool | None = None,
    langsmith_project: str | None = None,
) -> None:
    """Set up tracing once per process. Safe to call repeatedly.

    ``console`` defaults to the ``GLASS_GURU_TRACE_CONSOLE`` environment variable, so
    a developer can turn spans on without editing code and CI leaves them off.
    """
    global _configured
    if _configured:
        return

    show_console = (
        console
        if console is not None
        else os.environ.get("GLASS_GURU_TRACE_CONSOLE", "").lower() in {"1", "true", "yes"}
    )
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if show_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    project = langsmith_project or os.environ.get("LANGSMITH_PROJECT")
    if project and os.environ.get("LANGSMITH_API_KEY"):
        # LangGraph and the LangChain integrations read these directly; setting them
        # here keeps the wiring in one place rather than scattered across agents.
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ["LANGSMITH_PROJECT"] = project

    _configured = True


def langsmith_enabled() -> bool:
    """Whether LangSmith will actually receive anything."""
    return bool(os.environ.get("LANGSMITH_API_KEY")) and os.environ.get(
        "LANGSMITH_TRACING", ""
    ).lower() in {"1", "true", "yes"}


def _tracer() -> trace.Tracer:
    return trace.get_tracer(SERVICE_NAME)


def _flatten(prefix: str, value: Any) -> dict[str, Any]:
    """Span attributes must be scalars, so nested values are flattened, not dropped."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_flatten(f"{prefix}.{key}", item))
        return out
    if isinstance(value, (list, tuple, set)):
        return {prefix: ", ".join(str(v) for v in sorted(map(str, value))[:20])}
    if isinstance(value, (str, bool, int, float)) or value is None:
        return {prefix: value if value is not None else ""}
    return {prefix: str(value)}


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """A traced block, always tagged with the dispatch id in force.

    That tag is the whole point: it is what lets a solver run, three tool calls and a
    plan commit be filtered back into one episode.
    """
    configure()
    with _tracer().start_as_current_span(name) as active:
        active.set_attribute("dispatch_id", require_dispatch_id())
        for key, value in attributes.items():
            for flat_key, flat_value in _flatten(key, value).items():
                active.set_attribute(flat_key, flat_value)
        yield active


def record(**attributes: Any) -> None:
    """Attach facts to the current span once they are known.

    Solver metrics arrive at the end of the work, not the start; recording them here
    keeps the scheduler itself free of any tracing imports.
    """
    active = trace.get_current_span()
    if not active.is_recording():
        return
    for key, value in attributes.items():
        for flat_key, flat_value in _flatten(key, value).items():
            active.set_attribute(flat_key, flat_value)


def record_error(exc: BaseException) -> None:
    active = trace.get_current_span()
    if active.is_recording():
        active.record_exception(exc)
        active.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
