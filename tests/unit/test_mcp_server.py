"""MCP tool surface.

The tools are the boundary where the project's central claim is enforced rather than
stated: an agent can ask for a plan and record what a caller said, but cannot assert
an arrival time, declare a plan feasible, or commit something that fails its
invariants. These tests check the boundary holds and that failures come back as
structured values a small model can act on rather than prose it will paraphrase.
"""

from __future__ import annotations

from typing import Any

import pytest

from glass_guru.fixtures.sample_business import seed_events
from glass_guru.persistence.log import Workspace

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mcp_workspace(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "ws")
    workspace.seed(seed_events())
    monkeypatch.setenv("GLASS_GURU_WORKSPACE", str(tmp_path / "ws"))
    # Real road distances from the committed snapshot: offline, and identical here to
    # what a deployment would compute.
    monkeypatch.setenv("GLASS_GURU_TRAVEL", "frozen")
    return workspace


async def call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a tool and unwrap its structured payload.

    A union return type is wrapped under "result" by the SDK, and a tool that asks
    for elicitation returns a different shape entirely - none of ours do, so that is
    an assertion rather than a branch.
    """
    from mcp.types import CallToolResult

    from glass_guru.mcp_server.server import mcp

    result = await mcp.call_tool(name, args or {})
    assert isinstance(result, CallToolResult), f"{name} asked for input unexpectedly"
    assert result.structured_content is not None, f"{name} returned no structured output"
    payload: dict[str, Any] = result.structured_content["result"]
    return payload


# --------------------------------------------------------------------- surface


async def test_tools_are_exposed_with_closed_schemas(mcp_workspace):
    from glass_guru.mcp_server.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_world_state",
        "suggest_booking_slots",
        "plan_week",
        "repair_plan",
        "record_event",
        "get_plan_diff",
    }
    for tool in tools:
        assert tool.description, f"{tool.name} has no description for the model to read"


async def test_no_tool_can_assert_a_schedule(mcp_workspace):
    """The whole point. There is no way in through this surface to declare an arrival
    time, override feasibility, or force a plan past its invariants."""
    from glass_guru.mcp_server.server import mcp

    names = {t.name for t in await mcp.list_tools()}
    forbidden = {"set_arrival", "force_commit", "override_invariants", "set_plan"}
    assert not (names & forbidden)


# ----------------------------------------------------------------------- state


async def test_world_state_reports_the_roster(mcp_workspace):
    world = await call("get_world_state")
    assert len(world["workers"]) == 6
    assert len(world["vans"]) == 4
    assert len(world["jobs"]) == 10
    assert all(w["certifications"] for w in world["workers"])


async def test_world_state_warns_that_costs_are_uncalibrated(mcp_workspace):
    """An agent quoting a price should know the parameters behind it are guesses."""
    world = await call("get_world_state")
    assert any("estimate" in note for note in world["notes"])


# ---------------------------------------------------------------------- planning


async def test_plan_week_reports_feasibility_and_does_not_commit_by_default(
    mcp_workspace,
):
    plan = await call("plan_week")
    assert plan["feasible"] is True
    assert plan["violations"] == []
    assert plan["committed"] is False
    assert plan["jobs_scheduled"] == 10


async def test_plan_week_can_commit(mcp_workspace):
    plan = await call("plan_week", {"commit": True})
    assert plan["committed"] is True
    assert mcp_workspace.plans.head() is not None


async def test_unserved_jobs_come_with_reasons_already_computed(mcp_workspace):
    """Why a job could not be scheduled is arithmetic, so the engine answers it. An
    agent left to infer it would eventually infer it wrong and say so confidently."""
    plan = await call("plan_week", {"start_date": "2026-09-21"})
    for item in plan["unserved"]:
        assert item["reason"] and item["detail"]


# ----------------------------------------------------------------------- booking


async def test_booking_slots_are_priced_and_explained(mcp_workspace):
    # Quote the exact site of an existing job so the frozen snapshot covers every leg.
    # Geohash-7 cells are about 150m, so an approximated coordinate lands elsewhere.
    from glass_guru.fixtures.sample_business import JOBS

    chen = next(j for j in JOBS if j.id == "j-402").location

    await call("plan_week", {"commit": True})
    booking = await call(
        "suggest_booking_slots",
        {
            "service_type": "residential_window_replacement",
            "duration_minutes": 90,
            "latitude": chen.lat,
            "longitude": chen.lon,
            "required_certifications": ["residential_glazing"],
        },
    )
    assert booking["slots"]
    costs = [s["marginal_cost"] for s in booking["slots"]]
    assert costs == sorted(costs)
    assert all(s["reason"] for s in booking["slots"])


async def test_an_uncached_address_explains_itself_instead_of_crashing(mcp_workspace):
    """A brand-new address is exactly what a frozen snapshot cannot cover. An agent
    quoting one deserves a remedy, not an unhandled exception it will paraphrase."""
    result = await call(
        "suggest_booking_slots",
        {
            "service_type": "screen_repair",
            "duration_minutes": 45,
            "latitude": 47.1234,
            "longitude": -122.9876,
        },
    )
    assert result["error"] == "CacheMiss"
    assert "warm" in result["remedy"]


async def test_booking_without_a_location_is_refused_with_a_remedy(mcp_workspace):
    result = await call(
        "suggest_booking_slots",
        {"service_type": "screen_repair", "duration_minutes": 45},
    )
    assert result["error"] == "MissingLocation"
    assert "latitude" in result["remedy"]


async def test_an_unknown_service_type_fails_structurally(mcp_workspace):
    result = await call(
        "suggest_booking_slots",
        {
            "service_type": "teleportation",
            "duration_minutes": 45,
            "latitude": 47.6,
            "longitude": -122.3,
        },
    )
    assert "error" in result and result["detail"]


# ------------------------------------------------------------------------ events


async def test_recording_an_event_changes_the_world(mcp_workspace):
    ack = await call(
        "record_event",
        {"kind": "van-unavailable", "target": "van-1", "at": "10:40", "reason": "wont start"},
    )
    assert ack["recorded"] is True
    assert ack["dispatch_id"]

    world = await call("get_world_state")
    van = next(v for v in world["vans"] if v["id"] == "van-1")
    assert van["available_today"] is False


async def test_an_incomplete_event_is_refused_rather_than_guessed(mcp_workspace):
    """The log is the source of truth, so a malformed entry is worse than a refusal."""
    result = await call("record_event", {"kind": "job-overran", "target": "j-402"})
    assert result["error"] == "EventArgumentError"
    assert "--minutes" in result["detail"]


async def test_an_unknown_event_kind_lists_the_known_ones(mcp_workspace):
    result = await call("record_event", {"kind": "abduction", "target": "j-402"})
    assert "known events" in result["detail"]


# ------------------------------------------------------------------------ repair


async def test_repair_offers_priced_trade_offs_with_autonomy_verdicts(mcp_workspace):
    await call("plan_week", {"commit": True})
    await call("record_event", {"kind": "van-unavailable", "target": "van-1", "at": "10:40"})

    repair = await call("repair_plan")
    assert repair["candidates"]
    assert repair["recommended"]
    for candidate in repair["candidates"]:
        assert candidate["autonomy"] in {"auto_apply", "escalate"}
        assert candidate["blast_radius"] in {"internal", "crew_only", "customer_visible"}
        for change in candidate["diff"]:
            assert isinstance(change["needs_customer_call"], bool)


async def test_repairing_without_a_committed_plan_says_what_to_do(mcp_workspace):
    result = await call("repair_plan")
    assert result["error"] == "ServiceError"
    assert "commit" in result["remedy"]
