"""Quickstart: trace calls, build a dataset, run two versions, compare them with CIs, gate.

Runs offline: the "model" is a plain function. Swap in your real LLM call; with
``tirkit.instrument()`` OpenAI and Anthropic calls inside it are traced automatically.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import tirkit
from tirkit.datasets import Dataset, Example
from tirkit.report import render_compare, render_run
from tirkit.runner import compare, gate, run
from tirkit.scorers import ExactMatch, RuleScorer, rules

CAPITALS = {
    "France": "Paris",
    "Japan": "Tokyo",
    "Kenya": "Nairobi",
    "Peru": "Lima",
    "Canada": "Ottawa",
    "Egypt": "Cairo",
    "Norway": "Oslo",
    "Chile": "Santiago",
    "Ghana": "Accra",
    "India": "New Delhi",
}


@tirkit.trace(kind="llm", name="answer-v1")
def answer_v1(inp: dict) -> str:
    return CAPITALS[inp["country"]]


@tirkit.trace(kind="llm", name="answer-v2")
def answer_v2(inp: dict) -> str:
    # A "regression": the new prompt answers in a sentence for some countries.
    city = CAPITALS[inp["country"]]
    return f"The capital is {city}." if len(inp["country"]) <= 5 else city


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "quickstart.db"
    client = tirkit.init(path=db)
    ds = Dataset(
        "capitals",
        [Example({"country": c}, expected=city) for c, city in CAPITALS.items()] * 5,
    )
    client.store.save_dataset(ds)
    scorers = [ExactMatch(), RuleScorer([rules.max_length(20)], name="short")]
    base = run(ds, answer_v1, scorers, name="v1", store=client.store)
    cand = run(ds, answer_v2, scorers, name="v2", store=client.store)
    print(render_run(base), "\n")
    print(render_run(cand), "\n")
    passed = gate(base, cand, "exact_match")
    print(render_compare(compare(base, cand), base, cand, gate_scorer="exact_match", passed=passed))
    print(f"\nTraces stored in {db}: {client.store.count_traces()}")
    client.shutdown()


if __name__ == "__main__":
    main()
