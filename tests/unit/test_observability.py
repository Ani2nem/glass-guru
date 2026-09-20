"""Correlation and tracing.

Two properties matter. A dispatch id must survive nesting and restore cleanly, because
a tool call made while handling a disruption belongs to that disruption. And the whole
layer must be inert without configuration, because an observability layer that made
the test suite need network would simply be switched off.
"""

from __future__ import annotations

from glass_guru.obs.correlation import (
    current_dispatch_id,
    dispatch,
    new_dispatch_id,
    require_dispatch_id,
)
from glass_guru.obs.tracing import langsmith_enabled, record, span


def test_no_dispatch_id_outside_a_block():
    assert current_dispatch_id() is None


def test_a_block_sets_and_restores_the_id():
    with dispatch("d-outer") as active:
        assert active == "d-outer"
        assert current_dispatch_id() == "d-outer"
    assert current_dispatch_id() is None


def test_nesting_keeps_the_outer_id():
    """A tool call made while handling a disruption is part of that disruption, not a
    new one. Minting a fresh id here would split one episode into several traces."""
    with dispatch("d-outer"), dispatch() as inner:
        assert inner == "d-outer"


def test_an_explicit_inner_id_wins():
    with dispatch("d-outer"), dispatch("d-inner") as inner:
        assert inner == "d-inner"
    assert current_dispatch_id() is None


def test_minted_ids_are_distinct():
    assert new_dispatch_id() != new_dispatch_id()


def test_orphan_work_still_gets_an_id():
    """Untraced work is worth tracing; it just will not join up, which is a signal."""
    orphan = require_dispatch_id()
    assert orphan.startswith("orphan-")


def test_spans_are_inert_without_configuration():
    """No exporter, no exception, no network."""
    with dispatch("d-1"), span("test.span", foo="bar"):
        record(count=3, nested={"a": 1})


def test_langsmith_is_off_without_an_api_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert langsmith_enabled() is False


def test_span_attributes_flatten_nested_values():
    """Span attributes must be scalars. Solver metrics arrive as a dict, and dropping
    them rather than flattening would lose exactly the numbers worth keeping."""
    from glass_guru.obs.tracing import _flatten

    flat = _flatten("metrics", {"scheduled": 5, "inner": {"gap": 0.1}})
    assert flat == {"metrics.scheduled": 5, "metrics.inner.gap": 0.1}


def test_sequences_are_summarised_not_dropped():
    from glass_guru.obs.tracing import _flatten

    flat = _flatten("kinds", ["van_unavailable", "job_confirmed"])
    assert "van_unavailable" in flat["kinds"]
