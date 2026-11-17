"""CrewAI listener: real CrewAI event classes and event bus, no LLM calls. CrewAI is heavy and
not in the dev extra; this file is skipped without it (run it in an environment with ``crewai``).
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

pytest.importorskip("crewai.events")

from crewai import events  # noqa: E402
from crewai.events import crewai_event_bus  # noqa: E402

import tirkit  # noqa: E402
from tirkit.integrations import crewai as mod  # noqa: E402


@pytest.fixture(autouse=True)
def _instrumented():
    mod._tracer = None
    assert tirkit.instrument("crewai") == ["crewai"]
    yield
    mod._tracer = None


def agent_and_task(monkeypatch):
    from crewai import Agent, Task

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-used")
    agent = Agent(role="Support", goal="Help", backstory="A support agent", llm="gpt-4o-mini")
    task = Task(description="Answer the customer", expected_output="An answer", agent=agent)
    return agent, task


def emit(event) -> None:
    crewai_event_bus.emit(None, event)


def wait_for(sink, n: int) -> None:
    for _ in range(100):
        if len(sink.spans) >= n:
            return
        time.sleep(0.05)


def test_crew_run_nests_agent_tool_and_llm_in_one_trace(sink, monkeypatch) -> None:
    a, task = agent_and_task(monkeypatch)
    crew = events.CrewKickoffStartedEvent(crew_name="support", crew=None, inputs={"q": "refund?"})
    agent = events.AgentExecutionStartedEvent(
        agent=a,
        task=task,
        tools=[],
        task_prompt="Answer the customer",
        parent_event_id=crew.event_id,
    )
    llm = events.LLMCallStartedEvent(
        model="gpt-4o-mini",
        call_id="c1",
        messages=[{"role": "user", "content": "refund?"}],
        temperature=0.2,
        parent_event_id=agent.event_id,
    )
    tool = events.ToolUsageStartedEvent(
        tool_name="lookup",
        tool_args={"id": 7},
        tool_class="Lookup",
        agent_key="a",
        parent_event_id=agent.event_id,
    )
    # Delivery order is not guaranteed: send the ends first, and children before parents.
    emit(
        events.LLMCallCompletedEvent(
            model="gpt-4o-mini",
            call_id="c1",
            messages="x",
            response="Sure.",
            call_type="llm_call",
            usage={"prompt_tokens": 20, "completion_tokens": 5},
            started_event_id=llm.event_id,
        )
    )
    emit(
        events.ToolUsageFinishedEvent(
            tool_name="lookup",
            tool_args={"id": 7},
            tool_class="Lookup",
            agent_key="a",
            output="shipped",
            started_at=datetime.now(),
            finished_at=datetime.now(),
            from_cache=False,
            started_event_id=tool.event_id,
        )
    )
    emit(llm)
    emit(tool)
    emit(
        events.AgentExecutionCompletedEvent(
            agent=a, task=task, output="Done", started_event_id=agent.event_id
        )
    )
    emit(agent)
    emit(
        events.CrewKickoffCompletedEvent(
            crew_name="support",
            crew=None,
            output="Done",
            total_tokens=25,
            started_event_id=crew.event_id,
        )
    )
    emit(crew)
    wait_for(sink, 4)
    assert len(sink.spans) == 4
    crew_s = next(s for s in sink.spans if s.kind == "chain")
    agent_s = next(s for s in sink.spans if s.kind == "agent")
    llm_s = next(s for s in sink.spans if s.kind == "llm")
    tool_s = next(s for s in sink.spans if s.kind == "tool")
    assert (
        crew_s.parent_id is None
        and crew_s.input == {"q": "refund?"}
        and crew_s.attributes["total_tokens"] == 25
    )
    assert agent_s.parent_id == crew_s.span_id and agent_s.output == "Done"
    assert llm_s.parent_id == agent_s.span_id and tool_s.parent_id == agent_s.span_id
    assert len({s.trace_id for s in sink.spans}) == 1
    assert llm_s.model == "gpt-4o-mini" and llm_s.model_params == {"temperature": 0.2}
    assert llm_s.usage == {"input_tokens": 20, "output_tokens": 5} and llm_s.cost_usd > 0
    assert llm_s.output == "Sure." and tool_s.input == {"id": 7} and tool_s.output == "shipped"
    assert not mod._tracer._starts and not mod._tracer._ends and not mod._tracer._waiting


def test_failed_llm_call_is_an_error_span(sink) -> None:
    start = events.LLMCallStartedEvent(model="gpt-4o-mini", call_id="c9", messages=[])
    emit(start)
    emit(
        events.LLMCallFailedEvent(
            model="gpt-4o-mini", call_id="c9", error="rate limited", started_event_id=start.event_id
        )
    )
    wait_for(sink, 1)
    s = sink.spans[0]
    assert s.status == "error" and s.error == "rate limited"


def test_instrument_subscribes_once() -> None:
    first = mod._tracer
    assert tirkit.instrument("crewai") == ["crewai"]
    assert mod._tracer is first
