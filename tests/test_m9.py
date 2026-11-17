"""Online evals, alerts and judge reliability (M9)."""

from __future__ import annotations

import time

import pytest

from tirkit.datasets import Example
from tirkit.judge import LLMJudge
from tirkit.online import Evaluators, sampled, spend_today
from tirkit.reliability import flip_rate, judge_labels, reliability_report
from tirkit.tracing import Span


def _ingest(store, n: int, *, output='{"ok": true}', ago: float = 60, error_every: int = 0) -> None:
    now = time.time()
    spans = []
    for i in range(n):
        s = Span(
            name="agent",
            start=now - ago + i * 0.01,
            input={"q": f"question {i}"},
            output=output,
            status="error" if error_every and i % error_every == 0 else "ok",
        )
        s.end = s.start + 0.2
        spans.append(s)
    store.save_spans(spans)


def test_sampling_is_deterministic_and_close_to_rate() -> None:
    ids = [f"{i:032x}" for i in range(5000)]
    picks = [sampled(t, "ev_x", 0.2) for t in ids]
    assert picks == [sampled(t, "ev_x", 0.2) for t in ids]
    assert 0.17 < sum(picks) / len(picks) < 0.23
    assert all(sampled(t, "ev_x", 1.0) for t in ids[:50])


def test_rule_evaluator_scores_new_traces_once(store) -> None:
    ev = Evaluators(store)
    eid = ev.create("valid-json", {"spec": "json_valid"}, sampling_rate=1.0, backfill_s=3600)
    _ingest(store, 10)
    e = ev.items()[0]
    stats = ev.process(e, budget_usd=None)
    assert stats["scored"] == 10
    assert ev.process(ev.items()[0], budget_usd=None)["scored"] == 0  # cursor moved
    scores = store.query_scores(name="valid-json")
    assert len(scores) == 10 and {s["source"] for s in scores} == {"rule"}
    assert eid.startswith("ev_")


def test_judge_evaluator_respects_daily_budget(store, fake_llm) -> None:
    from tirkit import pricing

    pricing.register_price("fake-judge", input_tokens=0.001, output_tokens=0.001)  # $0.015 per call
    ev = Evaluators(store)
    ev.create(
        "quality",
        {"spec": "judge:correctness", "model": "fake-judge"},
        sampling_rate=1.0,
        backfill_s=3600,
    )
    _ingest(store, 12)
    stats = ev.process(ev.items()[0], budget_usd=0.05)
    assert stats["scored"] == 4 and stats["skipped_budget"] == 8  # 4 × $0.015 ≥ $0.05
    assert spend_today(store) == pytest.approx(0.06)
    s = store.query_scores(name="quality")[0]
    assert s["value"] == 4.0 and s["source"] == "llm_judge"


def test_alert_fires_and_resolves_with_webhook(store, webhook) -> None:
    from tirkit.alerts import Alerts

    url, received = webhook
    a = Alerts(store)
    a.create(
        "errors",
        {"metric": "error_rate", "op": ">", "threshold": 0.2, "window_s": 600},
        {"type": "webhook", "url": url},
    )
    _ingest(store, 10, error_every=2)  # 50% errors
    assert a.check(a.items()[0]) == "firing"
    assert "FIRING" in received[0]["text"]
    assert a.check(a.items()[0]) == "firing" and len(received) == 1  # no repeat while firing
    store.execute("DELETE FROM traces")
    _ingest(store, 10)
    assert a.check(a.items()[0]) == "ok" and "resolved" in received[1]["text"]
    assert [e["state"] for e in a.events()] == ["ok", "firing"]
    with pytest.raises(ValueError):
        a.create(
            "bad", {"metric": "nope", "op": ">", "threshold": 1}, {"type": "webhook", "url": url}
        )


def test_email_alert_without_smtp_records_failure(store, monkeypatch) -> None:
    from tirkit.alerts import Alerts

    monkeypatch.delenv("TIRKIT_SMTP_HOST", raising=False)
    a = Alerts(store)
    a.create(
        "volume",
        {"metric": "trace_count", "op": ">", "threshold": 1, "window_s": 600},
        {"type": "email", "to": "ops@example.com"},
    )
    _ingest(store, 3)
    a.check(a.items()[0])
    ev = a.events()[0]
    assert ev["delivered"] == 0 and "TIRKIT_SMTP_HOST" in ev["error"]


class PositionBiasedJudge:
    """Test double: scores = length of output in words (capped 1–5); prefers whatever is shown
    first in pairwise prompts, i.e. a pure position bias."""

    model = "biased"

    def __call__(self, prompt: str) -> str:
        if "<output_a>" in prompt:
            return '{"winner": "A", "reason": "first"}'
        out = prompt.split("<output>")[1].split("</output>")[0]
        n = max(1, min(5, len(out.split())))
        return f'{{"score": {n}, "reason": "length"}}'


def test_reliability_report_kappa_length_bias_and_flips() -> None:
    rows = []
    for i in range(20):
        words = 1 + i % 5
        rows.append(
            {"input": {"q": f"q{i}"}, "output": " ".join(["w"] * words), "labels": {"score": 3}}
        )
    for i in range(10):  # humans sometimes agree with length
        rows[i]["labels"]["score"] = 1 + i % 5
    scored = judge_labels(rows, LLMJudge(PositionBiasedJudge()), human_key="score")
    rep = reliability_report(scored, human_key="score")
    assert rep["n_judged"] == 20
    assert rep["length_corr_judge"] == pytest.approx(1.0)
    assert rep["flip_rate"] == 1.0 and rep["n_pairs"] == 10
    assert 0 < rep["exact_agreement"] < 1
    assert -1 <= rep["kappa"] <= 1 and -1 <= rep["weighted_kappa"] <= 1


def test_flip_rate_excludes_ties() -> None:
    pairs = [
        {"first": "a", "second": "a", "flipped": False},
        {"first": "a", "second": "b", "flipped": True},
        {"first": "tie", "second": "a", "flipped": False},
    ]
    assert flip_rate(pairs) == (0.5, 2)


@pytest.mark.server
def test_judge_report_job_on_queue_labels(tmp_path, fake_llm) -> None:
    from fastapi.testclient import TestClient

    from tests.test_server import basic, make_settings, new_key, setup_admin
    from tirkit import jobs
    from tirkit.server.app import create_app

    with TestClient(create_app(make_settings(tmp_path))) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        spans = []
        for i in range(6):
            s = Span(name="agent", input={"q": f"q{i}"}, output=f"answer {i}")
            s.end = s.start + 0.1
            spans.append(s.to_dict())
        c.post("/api/v1/ingest", json={"spans": spans}, headers=basic(pk, sk))
        c.put(
            f"/api/projects/{pid}/score-configs/correct",
            json={"data_type": "numeric", "min_value": 1, "max_value": 5},
        )
        qid = c.post(
            f"/api/projects/{pid}/queues", json={"name": "q", "score_names": ["correct"]}
        ).json()["queue_id"]
        c.post(f"/api/projects/{pid}/queues/{qid}/items", json={"filter": {}, "limit": 10})
        for i in range(6):
            item = c.get(f"/api/projects/{pid}/queues/{qid}/next").json()["item"]
            c.post(
                f"/api/projects/{pid}/queues/{qid}/items/{item['item_id']}/complete",
                json={"scores": {"correct": 4 if i % 2 else 3}},
            )
        r = c.post(
            f"/api/projects/{pid}/judge-reports",
            json={
                "queue_id": qid,
                "label": "correct",
                "model": "fake-judge",
                "rubric_name": "correctness",
                "rubric_text": "Score 1-5.",
            },
        )
        assert r.status_code == 202
        assert jobs.run_one(c.app.state.store, "w")
        reports = c.get(f"/api/projects/{pid}/judge-reports").json()
        rep = reports["reports"][0]
        assert rep["summary"]["n_judged"] == 6 and rep["summary"][
            "exact_agreement"
        ] == pytest.approx(0.5)
        assert reports["rubrics"][0]["name"] == "correctness"
        assert Example  # imported for type users


def test_periodic_lease_allows_one_holder(store) -> None:
    from tirkit.jobs import acquire_lease

    assert acquire_lease(store, "periodic:x", "w1", 60)
    assert acquire_lease(store, "periodic:x", "w1", 60)  # renew
    assert not acquire_lease(store, "periodic:x", "w2", 60)
    store.execute("UPDATE leases SET until = 0 WHERE name = 'periodic:x'")
    assert acquire_lease(store, "periodic:x", "w2", 60)  # expired lease can be taken
