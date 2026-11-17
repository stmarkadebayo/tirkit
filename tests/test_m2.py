"""Redaction, datasets, scorers and the LLM judge (M2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tirkit.datasets import Dataset, Example, from_traces, remove_duplicates
from tirkit.judge import LLMJudge, Rubric
from tirkit.media import Attachment
from tirkit.redact import redact
from tirkit.scorers import (
    CharacterErrorRate,
    ExactMatch,
    FunctionScorer,
    RuleScorer,
    TTSRoundTrip,
    WordErrorRate,
    rules,
    wer,
)
from tirkit.tracing import Span

FIXTURES = Path(__file__).parent / "fixtures"


def pii_cases() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "pii.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", pii_cases(), ids=lambda c: c["placeholder"] + ":" + c["text"][:20])
def test_redaction_fixture(case: dict) -> None:
    out = redact(case["text"])
    for secret in case["must_not_contain"]:
        assert secret not in out, out
    assert case["placeholder"] in out, out


@pytest.mark.parametrize(
    "text",
    [
        "The meeting is in 2024 at 10:30.",
        "Version 3.11.4 is out",
        "Order A1002 costs 39 dollars",
        "Call me in 5 minutes",
    ],
)
def test_redaction_leaves_ordinary_text(text: str) -> None:
    assert redact(text) == text


def test_dataset_jsonl_and_csv_round_trip(tmp_path) -> None:
    ds = Dataset("d", [Example({"q": "a"}, expected="b", metadata={"m": 1}), Example({"q": "c"})])
    ds.to_jsonl(tmp_path / "d.jsonl")
    back = Dataset.from_jsonl(tmp_path / "d.jsonl")
    assert back.version == ds.version
    (tmp_path / "d.csv").write_text("question,expected,metadata.src\nhi,HI,x\n")
    csv_ds = Dataset.from_csv(tmp_path / "d.csv")
    assert csv_ds.examples[0] == Example({"question": "hi"}, "HI", {"src": "x"})


def test_store_dataset_versions(store) -> None:
    ds = Dataset("faq", [Example({"q": "a"}, "A")])
    v1 = store.save_dataset(ds)
    assert store.save_dataset(ds) == v1  # idempotent
    ds2 = Dataset("faq", [*ds.examples, Example({"q": "b"}, "B")])
    v2 = store.save_dataset(ds2)
    assert v2 != v1
    assert store.load_dataset("faq", v1).examples == ds.examples
    assert len(store.load_dataset("faq")) == 2  # latest
    assert {d["name"] for d in store.list_datasets()} == {"faq"}
    with pytest.raises(KeyError):
        store.load_dataset("nope")


def test_near_duplicates_removed() -> None:
    base = "how long do i have to return an item that i bought last week from the store"
    exs = [
        Example({"q": base}),
        Example({"q": base.upper()}),  # exact after normalising
        Example({"q": base + " please"}),  # near duplicate
        Example({"q": "what is the warranty on the battery and the motor of the v2 vacuum"}),
    ]
    kept = remove_duplicates(exs, threshold=0.9)
    assert [e.input["q"] for e in kept] == [base, exs[3].input["q"]]


def test_from_traces_redacts_and_keeps_reference() -> None:
    spans = [
        Span(name="agent", input={"question": "email me at a.b@example.com"}, output="ok"),
        Span(name="agent", input="plain string input", output="fine"),
        Span(name="agent", input=None),
    ]
    ds = from_traces(spans)
    assert len(ds) == 2
    assert ds.examples[0].input == {"question": "email me at [EMAIL]"}
    assert ds.examples[0].metadata["reference_output"] == "ok"
    assert ds.examples[1].input == {"input": "plain string input"}
    assert ds.examples[0].expected is None
    regression = from_traces(spans, expected_from_output=True)
    assert regression.examples[0].expected == "ok"


def test_exact_match_and_rules() -> None:
    ex = Example({"q": "x"}, expected="Paris")
    assert ExactMatch().score(ex, "Paris").value == 1.0
    assert ExactMatch(normalise=True).score(ex, " paris. ").value == 1.0
    scorer = RuleScorer(
        [
            rules.max_length(10),
            rules.banned_phrases(["as an AI"]),
            rules.json_schema(
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
            ),
        ]
    )
    s = scorer.score(ex, '{"a": "no"}')
    assert s.value == pytest.approx(1 / 3)
    assert "max_length" in s.reason and "json_schema" in s.reason


def test_function_scorer_errors_are_failures() -> None:
    def boom(ex, out):
        raise RuntimeError("x")

    s = FunctionScorer(boom).score(Example({}), "o")
    assert s.value is None and "RuntimeError" in s.error


def test_wer_and_cer() -> None:
    assert wer("the cat sat on the mat", "the cat sat on mat") == pytest.approx(1 / 6)
    assert wer("Hello, world!", "hello world") == 0.0
    ex = Example({"audio": "..."}, expected="turn on the lights")
    assert WordErrorRate().score(ex, {"text": "turn off the lights"}).value == pytest.approx(0.25)
    assert WordErrorRate.higher_is_better is False
    assert CharacterErrorRate().score(ex, "turn on the lights").value == 0.0
    assert WordErrorRate().score(Example({}), "x").error


def test_tts_round_trip_uses_reference_asr() -> None:
    heard = {}

    def transcribe(audio: bytes, mime: str | None) -> str:
        heard["bytes"] = audio
        return "good morning everyone"

    s = TTSRoundTrip(transcribe).score(
        Example({"text": "Good morning, everyone."}),
        {"audio": Attachment(data=b"RIFFxx", mime="audio/wav")},
    )
    assert s.value == 0.0 and heard["bytes"] == b"RIFFxx"


class FakeModel:
    """Scripted judge model: returns replies in order and records prompts."""

    model = "fake-judge"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.images: list = []
        self.last_cost = 0.01

    def __call__(self, prompt: str, images=None) -> str:
        self.prompts.append(prompt)
        self.images.append(images)
        return self.replies.pop(0)


def test_judge_parses_score_and_reason() -> None:
    m = FakeModel(['Thinking... {"score": 4, "reason": "close"}'])
    s = LLMJudge(m, "correctness").score(Example({"q": "2+2"}, expected="4"), "four")
    assert s.value == 4.0 and s.reason == "close"
    assert "<expected-" in m.prompts[0] and "5: fully correct" in m.prompts[0]  # nonce-tagged


def test_judge_retries_once_then_records_failure() -> None:
    m = FakeModel(["no json here", '{"score": 9}'])  # second reply is out of range
    s = LLMJudge(m).score(Example({"q": "x"}), "y")
    assert s.value is None and "could not parse" in s.error
    assert len(m.prompts) == 2


def test_judge_cache_and_budget(store) -> None:
    from tirkit.judge import Budget, BudgetExceeded

    m = FakeModel(['{"score": 5, "reason": "ok"}'])
    j = LLMJudge(m, cache=store, budget=Budget(1.0))
    ex = Example({"q": "x"})
    assert j.score(ex, "y").value == 5
    assert j.score(ex, "y").value == 5  # served from cache, no second call
    assert len(m.prompts) == 1
    poor = LLMJudge(FakeModel(['{"score": 5}'] * 3), budget=Budget(0.01))
    poor.score(ex, "a")
    with pytest.raises(BudgetExceeded):
        poor.score(ex, "b")


def test_judge_sends_images_for_vision_tasks() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 10
    m = FakeModel(['{"score": 3, "reason": "meh"}'])
    LLMJudge(m).score(
        Example({"image": Attachment(data=png, mime="image/png"), "q": "what?"}), "a cat"
    )
    assert m.images[0] == [(png, "image/png")]
    assert "[image/png attached]" in m.prompts[0] or "[media attached]" in m.prompts[0]


def test_pairwise_swap_detects_position_bias() -> None:
    # Always prefers whichever output is shown first: a pure position bias.
    m = FakeModel(['{"winner": "A"}', '{"winner": "A"}'])
    r = LLMJudge(m).pairwise_detail(Example({"q": "x"}), "answer one", "answer two")
    assert r.first == "a" and r.second == "b" and r.flipped and r.verdict == "tie"
    consistent = FakeModel(['{"winner": "A"}', '{"winner": "B"}'])
    r2 = LLMJudge(consistent).pairwise_detail(Example({"q": "x"}), "good", "bad")
    assert r2.verdict == "a" and not r2.flipped


def test_rubric_version_changes_with_text() -> None:
    assert Rubric("r", "a").version != Rubric("r", "b").version
