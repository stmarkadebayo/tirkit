"""OpenAI Agents SDK processor, driven through the SDK's own tracing API (no network)."""

from __future__ import annotations

import pytest

agents = pytest.importorskip("agents")

from agents.tracing import (  # noqa: E402
    agent_span,
    function_span,
    generation_span,
    guardrail_span,
    handoff_span,
    trace,
)
from agents.tracing.setup import get_trace_provider  # noqa: E402

from tirkit.integrations.openai_agents import TirkitTracingProcessor  # noqa: E402


@pytest.fixture
def processor():
    p = TirkitTracingProcessor()
    provider = get_trace_provider()
    provider.set_processors([p])
    yield p
    provider.set_processors([])


def test_trace_with_agent_generation_tool_handoff_and_guardrail(sink, processor) -> None:
    with trace("support", group_id="conv-1", metadata={"tenant": "acme"}):
        with agent_span(name="triage", handoffs=["billing"], tools=["lookup"]):
            with generation_span(
                input=[{"role": "user", "content": "refund?"}],
                output=[{"role": "assistant", "content": "Let me check."}],
                model="gpt-4o-mini",
                model_config={"temperature": 0.2},
                usage={"input_tokens": 20, "output_tokens": 5},
            ):
                pass
            with function_span(name="lookup", input='{"id": 7}', output="shipped"):
                pass
            with handoff_span(from_agent="triage", to_agent="billing"):
                pass
            with guardrail_span(name="pii", triggered=True):
                pass
    by = {s.name: s for s in sink.spans}
    root, agent = by["support"], by["triage"]
    assert root.kind == "chain" and root.session_id == "conv-1" and root.parent_id is None
    assert root.attributes["metadata"] == {"tenant": "acme"}
    assert agent.kind == "agent" and agent.parent_id == root.span_id
    assert agent.attributes["handoffs"] == ["billing"] and agent.attributes["tools"] == ["lookup"]
    gen = by["generation"]
    assert gen.kind == "llm" and gen.parent_id == agent.span_id and gen.model == "gpt-4o-mini"
    assert gen.usage == {"input_tokens": 20, "output_tokens": 5} and gen.cost_usd > 0
    assert gen.input["messages"][0]["content"] == "refund?" and gen.model_params == {
        "temperature": 0.2
    }
    tool = by["lookup"]
    assert tool.kind == "tool" and tool.output == "shipped" and tool.parent_id == agent.span_id
    assert by["handoff: triage -> billing"].attributes["to_agent"] == "billing"
    guard = by["pii"]
    assert guard.kind == "guardrail" and guard.output == {"triggered": True}
    assert len({s.trace_id for s in sink.spans}) == 1  # one trace, ids shared with the SDK
    assert not processor._spans  # everything closed


def test_errors_are_recorded(sink, processor) -> None:
    with trace("t"):
        with function_span(name="boom") as sp:
            sp.set_error({"message": "tool failed", "data": None})
    s = next(s for s in sink.spans if s.name == "boom")
    assert s.status == "error" and s.error == "tool failed"


def test_nests_under_an_existing_tirkit_span(sink, processor) -> None:
    import tirkit

    with tirkit.span("request") as req:
        with trace("run"):
            pass
    run = next(s for s in sink.spans if s.name == "run")
    assert run.parent_id == req.span_id


def test_instrument_registers_the_processor(sink) -> None:
    import tirkit
    from tirkit.integrations import openai_agents as mod

    mod._installed = None
    provider = get_trace_provider()
    provider.set_processors([])
    assert tirkit.instrument("openai_agents") == ["openai_agents"]
    with trace("via-instrument"):
        pass
    assert any(s.name == "via-instrument" for s in sink.spans)
    provider.set_processors([])
