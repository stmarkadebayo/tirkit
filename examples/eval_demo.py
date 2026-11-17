"""Evaluation demo: run two versions of the RAG agent over a dataset and compare them.

Works locally (SQLite) or against a server (set TIRKIT_HOST / TIRKIT_PUBLIC_KEY / TIRKIT_SECRET_KEY).

    python examples/rag_demo.py              # traces + a dataset mined from them (local)
    python examples/eval_demo.py --dataset support-faq

The scorer is token F1 against the reference answer recorded on the source trace; v2 retrieves
only the second-ranked document, a deliberate regression the comparison should catch.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import rag_demo  # noqa: E402

import tirkit  # noqa: E402
from tirkit.datasets import Example  # noqa: E402
from tirkit.report import render_compare  # noqa: E402
from tirkit.runner import compare, run  # noqa: E402
from tirkit.scorers import FunctionScorer, RuleScorer, output_text, rules  # noqa: E402

_W = re.compile(r"[a-z0-9]+")


def token_f1(example: Example, output: Any) -> float | None:
    ref = (
        example.expected
        if example.expected is not None
        else example.metadata.get("reference_output")
    )
    if ref is None:
        return None
    a, b = _W.findall(output_text(output).lower()), _W.findall(output_text(ref).lower())
    if not a or not b:
        return 0.0
    common = sum(min(a.count(w), b.count(w)) for w in set(a))
    if not common:
        return 0.0
    p, r = common / len(a), common / len(b)
    return 2 * p * r / (p + r)


def v1(inp: dict) -> str:
    return rag_demo.answer(inp["question"])


def v2(inp: dict) -> str:
    """Off-by-one retrieval bug: uses the second-ranked document only."""
    q = inp["question"]
    return rag_demo.call_llm(q, rag_demo.retrieve(q, k=2)[1:], [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="support-faq")
    ap.add_argument("--db", default="demo.db")
    args = ap.parse_args()
    client = tirkit.init() if os.environ.get("TIRKIT_HOST") else tirkit.init(path=args.db)
    ds = client.store.load_dataset(args.dataset)
    scorers = [
        FunctionScorer(token_f1, name="token_f1"),
        RuleScorer([rules.max_length(400), rules.banned_phrases(["I don't know"])], name="format"),
    ]
    base = run(ds, v1, scorers, name="agent v1", store=client.store)
    cand = run(ds, v2, scorers, name="agent v2 (1 doc)", store=client.store)
    print(render_compare(compare(base, cand), base, cand))
    client.shutdown()


if __name__ == "__main__":
    main()
