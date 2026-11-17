"""Acceptance tests: the definition of done from SPEC.md. They fail until the feature is built."""

import pytest

from tirkit import stats

pytestmark = pytest.mark.acceptance


def test_cohen_kappa_known_value() -> None:
    # Observed agreement 0.75, chance agreement 0.5, so kappa = 0.5.
    assert stats.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)


def test_bootstrap_ci_contains_mean() -> None:
    lo, hi = stats.bootstrap_ci([0.0, 1.0] * 50, seed=1)
    assert lo < 0.5 < hi


def test_span_round_trip(tmp_path) -> None:
    from tirkit.store import SQLiteStore
    from tirkit.tracing import span

    store = SQLiteStore(tmp_path / "t.db")
    store.init_schema()
    with span("llm.call", store, model="x"):
        pass
    assert store.query_spans()[0].attributes["model"] == "x"


def test_dataset_version_is_content_hash() -> None:
    from tirkit.datasets import Dataset, Example

    a = Dataset("a", [Example({"q": 1}, expected=2)])
    b = Dataset("b", [Example({"q": 1}, expected=2)])
    assert a.version == b.version


def test_gate_blocks_regression() -> None:
    from tirkit.runner import RunResult, gate

    base = RunResult("d", "v", scores={"s": [1.0] * 100})
    worse = RunResult("d", "v", scores={"s": [1.0] * 60 + [0.0] * 40})
    assert gate(base, worse, "s") is False


def test_nested_spans_across_asyncio_tasks(tmp_path) -> None:
    """M1: nested spans across asyncio tasks keep their parents."""
    import asyncio

    import tirkit

    client = tirkit.init(path=tmp_path / "a.db")

    @tirkit.trace(name="step")
    async def step(i: int) -> int:
        await asyncio.sleep(0)
        return i

    async def main() -> None:
        with tirkit.span("root"):
            await asyncio.gather(*(step(i) for i in range(5)))

    asyncio.run(main())
    tirkit.flush()
    spans = client.store.query_spans()
    root = next(s for s in spans if s.name == "root")
    steps = [s for s in spans if s.name == "step"]
    client.shutdown()
    assert len(steps) == 5
    assert all(s.parent_id == root.span_id for s in steps)


def test_instrumented_calls_record_tokens_and_cost(tmp_path) -> None:
    """M1: instrumented OpenAI and Anthropic calls record tokens and cost."""
    httpx2 = pytest.importorskip("httpx2")
    openai = pytest.importorskip("openai")
    anthropic = pytest.importorskip("anthropic")
    import tirkit
    from tirkit.instrumentation import uninstrument

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx2.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"},
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    client = tirkit.init(path=tmp_path / "b.db")
    tirkit.instrument("openai", "anthropic")
    try:
        transport = httpx2.MockTransport(handler)
        openai.OpenAI(
            api_key="k", http_client=httpx2.Client(transport=transport)
        ).chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}])
        anthropic.Anthropic(
            api_key="k", http_client=httpx2.Client(transport=transport)
        ).messages.create(
            model="claude-opus-5-5", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )
        tirkit.flush()
        spans = {s.name: s for s in client.store.query_spans()}
    finally:
        uninstrument()
        client.shutdown()
    for name in ("openai.chat", "anthropic.messages"):
        assert spans[name].input_tokens == 100 and spans[name].output_tokens == 10
        assert spans[name].cost_usd is not None and spans[name].cost_usd > 0


def test_dataset_from_demo_traces_is_redacted(tmp_path) -> None:
    """M2: a dataset built from demo traces has PII redacted (fixture of known PII strings)."""
    import json
    import sys
    from pathlib import Path

    import tirkit
    from tirkit.datasets import from_traces

    sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
    import rag_demo

    client = tirkit.init(path=tmp_path / "demo.db")
    fixture = [
        json.loads(line)
        for line in (Path(__file__).parent / "fixtures" / "pii.jsonl").read_text().splitlines()
    ]
    for case in fixture:
        rag_demo.answer(case["text"])
    tirkit.flush()
    roots = client.store.query_spans({"name": "support-agent"}, limit=1000)
    ds = from_traces(roots, name="pii-check")
    client.store.save_dataset(ds)
    stored = json.dumps([e.__dict__ for e in client.store.load_dataset("pii-check").examples])
    client.shutdown()
    assert len(ds) == len(fixture)
    for case in fixture:
        for secret in case["must_not_contain"]:
            assert secret not in stored


def test_judge_parse_failures_are_recorded_not_scored() -> None:
    """M2: judge parse failures are recorded, not scored."""
    from tirkit.datasets import Example
    from tirkit.judge import LLMJudge

    replies = iter(["I think it's good", "Still no JSON, sorry"])
    s = LLMJudge(lambda prompt: next(replies)).score(Example({"q": "x"}), "y")
    assert s.value is None
    assert s.error


def test_moving_production_label_changes_app_without_redeploy(live_server) -> None:
    """M8: changing the production label changes what the app uses, no redeploy."""
    import httpx

    import tirkit

    url, _ = live_server
    c = httpx.Client(base_url=url)
    pid = c.post(
        "/api/setup", json={"email": "a@example.com", "password": "correct horse battery"}
    ).json()["project_id"]
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    keys = c.post(f"/api/projects/{pid}/keys", json={}).json()
    for text in ("v1: {{q}}", "v2: {{q}}"):
        c.post(
            f"/api/projects/{pid}/prompts/greet/versions", json={"kind": "text", "template": text}
        )
    c.put(f"/api/projects/{pid}/prompts/greet/labels/production", json={"version": 1})
    client = tirkit.init(host=url, public_key=keys["public_key"], secret_key=keys["secret_key"])

    def app_handler(q: str) -> str:  # the running app: fetches the prompt on each request
        return tirkit.get_prompt("greet", cache_ttl_s=0).compile(q=q)

    assert app_handler("hi") == "v1: hi"
    c.put(f"/api/projects/{pid}/prompts/greet/labels/production", json={"version": 2})
    assert app_handler("hi") == "v2: hi"
    client.shutdown()


def test_online_evals_score_sampled_traffic_within_budget(tmp_path, fake_llm) -> None:
    """M9: online evals score sampled demo traffic within budget."""
    import time

    from tirkit import pricing
    from tirkit.online import Evaluators, run_all, spend_today
    from tirkit.store import SQLiteStore
    from tirkit.tracing import Span

    root = SQLiteStore(tmp_path / "online.db")
    root.init_schema()
    root.execute(
        "INSERT INTO projects (project_id, name, settings, created) VALUES (?, ?, ?, ?)",
        ["p1", "demo", '{"judge_budget_usd_per_day": 0.1}', time.time()],
    )
    store = root.for_project("p1")
    pricing.register_price("fake-judge", input_tokens=0.001, output_tokens=0.001)
    ev = Evaluators(store)
    ev.create(
        "quality",
        {"spec": "judge:correctness", "model": "fake-judge"},
        sampling_rate=0.5,
        backfill_s=3600,
    )
    now = time.time()
    spans = []
    for i in range(60):
        s = Span(name="agent", start=now - 100 + i, input={"q": f"q{i}"}, output="a")
        s.end = s.start + 0.1
        spans.append(s)
    store.save_spans(spans)
    run_all(root)
    e = ev.items()[0]
    scored = len(store.query_scores(name="quality"))
    assert 0 < scored <= 7  # $0.015 per call, $0.10 budget -> at most 7 calls
    assert e["n_not_sampled"] > 0 and e["n_skipped_budget"] > 0
    assert spend_today(store) <= 0.1 + 0.015


def test_typescript_sdk_traces_node_demo_into_server(live_server) -> None:
    """M10: the TS SDK traces a Node demo into the server."""
    import os
    import shutil
    import subprocess
    from pathlib import Path

    import httpx

    sdk = Path(__file__).parents[1] / "sdk-ts"
    if not shutil.which("node") or not (sdk / "dist" / "index.js").exists():
        pytest.skip("node or the built TS SDK (cd sdk-ts && npm run build) is missing")
    url, _ = live_server
    c = httpx.Client(base_url=url)
    pid = c.post(
        "/api/setup", json={"email": "a@example.com", "password": "correct horse battery"}
    ).json()["project_id"]
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    keys = c.post(f"/api/projects/{pid}/keys", json={}).json()
    env = {
        **os.environ,
        "TIRKIT_HOST": url,
        "TIRKIT_PUBLIC_KEY": keys["public_key"],
        "TIRKIT_SECRET_KEY": keys["secret_key"],
    }
    out = subprocess.run(
        ["node", "examples/demo.mjs"], cwd=sdk, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    traces = c.get(f"/api/projects/{pid}/traces").json()
    assert traces["total"] == 3
    t = traces["items"][0]
    detail = c.get(f"/api/projects/{pid}/traces/{t['trace_id']}").json()
    assert {s["name"] for s in detail["spans"]} == {"node-agent", "retrieve", "llm"}
    assert t["release"] == "node-demo" and t["cost_usd"] > 0
    assert len(c.get(f"/api/projects/{pid}/sessions").json()) == 2
