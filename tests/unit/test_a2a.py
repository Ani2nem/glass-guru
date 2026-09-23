"""Agent-to-agent protocol.

A2A and MCP are doing different jobs, and the tests are shaped around the difference.
MCP reaches a tool - a typed function with a schema. A2A reaches an agent, which has
its own judgement and the right to come back and ask a question rather than answer.

That right is why the task lifecycle exists instead of a plain call, so `input-required`
gets more attention here than the happy path.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from glass_guru.agents.a2a.transport import (
    AgentNotFound,
    HttpA2ATransport,
    InProcessTransport,
)
from glass_guru.agents.a2a.types import (
    A2AMessage,
    AgentCard,
    AgentSkill,
    Role,
    Task,
    TaskState,
    submitted,
)
from glass_guru.agents.llm.base import LLMError, LLMRequest, LLMResponse
from glass_guru.agents.llm.factory import UnavailableLLMProvider, build_llm
from glass_guru.agents.llm.scripted import ScriptedLLMProvider
from glass_guru.agents.registry import (
    COORDINATOR_CARD,
    TRIAGE_CARD,
    TRIAGE_SKILL,
    TriageContext,
    build_transport,
)
from glass_guru.agents.structured import extract
from glass_guru.fixtures.sample_business import WEEK_START
from glass_guru.obs.correlation import dispatch

TZ = ZoneInfo("America/Chicago")


@pytest.fixture
def context(world) -> TriageContext:
    return TriageContext(world=world, on_date=WEEK_START, tz=TZ)


def clean_triage() -> ScriptedLLMProvider:
    return ScriptedLLMProvider.returning(
        {
            "events": [
                {
                    "kind": "van-unavailable",
                    "target": "van-3",
                    "at": "10:40",
                    "reason": "wont start",
                }
            ],
            "question": "",
            "summary": "Van 3 out from 10:40.",
        }
    )


# ------------------------------------------------------------------- discovery


def test_agents_are_found_by_capability_not_by_name(context):
    """The coordinator is written against "something that can triage", not against a
    module path."""
    transport = build_transport(clean_triage(), triage_context=context)
    assert transport.discover(TRIAGE_SKILL) == "triage"


def test_an_unknown_skill_says_what_is_available(context):
    transport = build_transport(clean_triage(), triage_context=context)
    with pytest.raises(AgentNotFound, match="disruption-triage"):
        transport.discover("time-travel")


def test_agent_cards_describe_their_skills():
    assert TRIAGE_CARD.handles(TRIAGE_SKILL)
    assert TRIAGE_CARD.description
    assert all(s.description for s in TRIAGE_CARD.skills)
    assert COORDINATOR_CARD.skills


def test_an_unregistered_agent_is_refused():
    transport = InProcessTransport()
    with pytest.raises(AgentNotFound):
        transport.send("ghost", submitted("hello"))


# ------------------------------------------------------------------ lifecycle


def test_a_clear_note_completes_with_an_artifact(context):
    transport = build_transport(clean_triage(), triage_context=context)
    with dispatch("d-1") as did:
        task = transport.send("triage", submitted("van 3 won't start", dispatch_id=did))

    assert task.state is TaskState.COMPLETED
    assert task.done and not task.needs_input
    artifact = task.artifact("events")
    assert artifact is not None
    assert len(artifact.data["events"]) == 1


def test_an_ambiguous_note_asks_rather_than_guesses(context):
    """The state that makes "ask rather than guess" expressible across an agent
    boundary. A plain function return has nowhere to put a question."""
    provider = ScriptedLLMProvider.returning(
        {"events": [], "question": "Which van is Dan on?", "summary": "Possible fault."}
    )
    transport = build_transport(provider, triage_context=context)
    with dispatch("d-2"):
        task = transport.send("triage", submitted("the van's making a noise"))

    assert task.state is TaskState.INPUT_REQUIRED
    assert task.needs_input
    assert not task.done
    assert task.status.message is not None
    assert "Which van" in task.status.message.text_content


def test_a_failing_agent_fails_its_task_without_crashing_the_caller(context):
    """One agent falling over must not take down the episode around it."""

    class Exploding:
        model_id = "boom"

        def complete(self, request: LLMRequest) -> LLMResponse:
            raise RuntimeError("model on fire")

    transport = build_transport(Exploding(), triage_context=context)
    with dispatch("d-3"):
        task = transport.send("triage", submitted("anything"))
    assert task.state is TaskState.FAILED
    assert task.done


def test_history_grows_with_the_agent_reply(context):
    transport = build_transport(clean_triage(), triage_context=context)
    with dispatch("d-4"):
        task = transport.send("triage", submitted("van 3 won't start"))
    assert len(task.history) == 2
    assert task.history[0].role is Role.USER
    assert task.history[-1].role is Role.AGENT


# --------------------------------------------------------------- correlation


def test_the_dispatch_id_travels_with_the_task(context):
    """An A2A hop lands in the same trace as the tool calls and solves either side."""
    transport = build_transport(clean_triage(), triage_context=context)
    with dispatch("d-correlated"):
        task = transport.send("triage", submitted("van 3 won't start"))
    assert task.metadata["dispatch_id"] == "d-correlated"


def test_a_task_sent_without_one_still_gets_an_id(context):
    transport = build_transport(clean_triage(), triage_context=context)
    with dispatch("d-ambient"):
        task = transport.send("triage", submitted("van 3 won't start"))
    assert task.metadata["dispatch_id"]


# ----------------------------------------------------------------- messages


def test_data_parts_carry_structured_content():
    message = A2AMessage.data(Role.AGENT, {"count": 2}, text="two things")
    assert message.data_content == {"count": 2}
    assert message.text_content == "two things"


def test_tasks_round_trip_through_json():
    """They must: the HTTP transport sends exactly this."""
    original = submitted("hello", dispatch_id="d-x")
    restored = Task.model_validate_json(original.model_dump_json())
    assert restored.id == original.id
    assert restored.metadata["dispatch_id"] == "d-x"


def test_agent_cards_round_trip_through_json():
    """The HTTP transport fetches these from the well-known path."""
    card = AgentCard(
        name="x", description="y", skills=[AgentSkill(id="s", name="S", description="d")]
    )
    assert AgentCard.model_validate_json(card.model_dump_json()).handles("s")


def test_http_transport_needs_an_endpoint():
    transport = HttpA2ATransport({})
    with pytest.raises(AgentNotFound):
        transport.send("triage", submitted("hello"))


# ------------------------------------------------------------ provider failure


def test_an_unreachable_model_is_not_reported_as_a_bad_answer():
    """Different problems need different messages. One is an operator's configuration
    issue, the other is a prompt or schema issue, and reporting them identically sends
    people to debug the wrong thing."""
    from glass_guru.agents.triage import TriageOutcome

    result = extract(UnavailableLLMProvider(), TriageOutcome, system="s", text="t", max_attempts=3)
    assert result.provider_failed
    assert result.escalated


def test_an_unreachable_model_does_not_inflate_the_repair_count():
    """Retrying an unreachable endpoint will not help, and claiming the full retry
    budget was spent would corrupt the repair-rate health signal."""
    from glass_guru.agents.triage import TriageOutcome

    result = extract(UnavailableLLMProvider(), TriageOutcome, system="s", text="t", max_attempts=3)
    assert result.attempts == 1
    assert result.repairs == 0


def test_a_bad_answer_is_not_reported_as_an_unreachable_model():
    from glass_guru.agents.triage import TriageOutcome

    provider = ScriptedLLMProvider.always({"events": [{"kind": "nope"}]})
    result = extract(provider, TriageOutcome, system="s", text="t", max_attempts=2)
    assert result.escalated
    assert not result.provider_failed
    assert result.attempts == 2


def test_triage_surfaces_the_configuration_problem(context):
    from glass_guru.agents.triage import triage

    with dispatch("d-5"):
        result = triage(
            UnavailableLLMProvider(),
            context.world,
            "van 3 down",
            on_date=WEEK_START,
            tz=TZ,
        )
    assert "unavailable" in result.question
    assert "credentials" in result.question


def test_the_factory_refuses_rather_than_pretending(monkeypatch):
    monkeypatch.setenv("GLASS_GURU_LLM", "unavailable")
    provider = build_llm()
    with pytest.raises(LLMError, match="no model provider configured"):
        provider.complete(LLMRequest(system="", messages=()))
