"""Shared fixtures."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request

from tirkit.store import SQLiteStore
from tirkit.tracing import Span, set_default_sink

# Tests that use these fixtures need the server extra; they are marked ``server`` automatically
# (tests that build the app inline carry ``@pytest.mark.server`` themselves). ``make test-core``
# runs everything else, ``make test-server`` runs these.
SERVER_FIXTURES = {"client", "live_server", "seeded", "server"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if SERVER_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.server)


class ListSink:
    """Collects finished spans in memory."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def save_span(self, span: Span) -> None:
        self.spans.append(span)

    def save_spans(self, spans: list[Span]) -> None:
        self.spans.extend(spans)

    def by_name(self, name: str) -> Span:
        return next(s for s in self.spans if s.name == name)


@pytest.fixture
def sink() -> Iterator[ListSink]:
    s = ListSink()
    set_default_sink(s)
    yield s
    set_default_sink(None)


@pytest.fixture
def store(tmp_path: Any) -> Iterator[SQLiteStore]:
    st = SQLiteStore(tmp_path / "tirkit.db")
    st.init_schema()
    yield st
    st.close()


def sse(events: list[Any], *, named: bool = False, done: bool = False) -> bytes:
    """Encode server-sent events (Anthropic names events; OpenAI ends with [DONE])."""
    out = []
    for e in events:
        if named:
            out.append(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n")
        else:
            out.append(f"data: {json.dumps(e)}\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server(tmp_path) -> Iterator[tuple[str, Any]]:
    import uvicorn

    from tests.test_server import make_settings
    from tirkit.server.app import create_app

    app = create_app(make_settings(tmp_path))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def fake_llm(monkeypatch) -> Iterator[str]:
    """An OpenAI-compatible server: echoes the last user message upper-cased, or answers
    judge prompts with a fixed score. Sets LLM_BASE_URL for the server-side providers."""
    import uvicorn

    app = FastAPI()

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"data": [{"id": "fake-small"}, {"id": "fake-judge"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        last = body["messages"][-1]["content"]
        text = last if isinstance(last, str) else " ".join(p.get("text", "") for p in last)
        if "grading the output" in text:
            reply = '<think>hmm</think>{"score": 4, "reason": "fine"}'
        else:
            reply = text.upper()
        return {
            "id": "x",
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/v1"
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    yield url
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A local HTTP endpoint that records JSON posts (for alert delivery)."""
    import uvicorn

    received: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request) -> dict[str, bool]:
        received.append(await request.json())
        return {"ok": True}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/hook", received
    server.should_exit = True
    thread.join(5)
