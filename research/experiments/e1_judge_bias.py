"""Experiment E1, position-bias half: does the LLM judge's preference flip when answer order flips?

No human labels are needed for this half (the kappa half needs the owner's labels; see
docs/E1.md). Protocol:

1. For each of the 50 questions in research/evals/acme_qa.jsonl, generate two answers from the same model
   with two prompts: BRIEF (one sentence) and DETAILED (3-5 sentences). Answers are cached.
2. For each question, the judge compares the two answers twice: once with BRIEF shown first, once
   with DETAILED shown first (pairwise prompt, SPEC §6.5), with the reference answer available.
3. Flip rate = share of pairs where the preferred answer changes with the order, ties in either
   order excluded. Also reported: how often the first-shown answer wins, and how often the longer
   (DETAILED) answer wins among consistent verdicts.
4. Run under rubric v0 (built-in `correctness`) and v1 (calibrated: explicit criteria, anchored
   examples, an instruction to ignore order and length). Every rubric version is stored.

    python research/experiments/e1_judge_bias.py --base-url http://localhost:1234/v1 --model qwen/qwen3.5-9b

Results go to research/experiments/results/e1_flip_rate.json.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from rag_demo import DOCS  # noqa: E402

from tirkit.datasets import Dataset, Example  # noqa: E402
from tirkit.judge import RUBRICS, LLMJudge, OpenAICompatibleJudge, Rubric  # noqa: E402
from tirkit.reliability import flip_rate  # noqa: E402

CONTEXT = "\n".join(f"[{k}] {v}" for k, v in DOCS.items())
BRIEF = "Answer the customer's question in one short sentence, using only the context."
DETAILED = (
    "Answer the customer's question thoroughly in 3 to 5 sentences, using only the context. "
    "Explain the relevant policy and any conditions."
)

RUBRIC_V1 = Rubric(
    "correctness",
    """Judge which answer is more CORRECT and COMPLETE for the question, using the reference answer
as ground truth.

Criteria, in order of importance:
1. Factual agreement with the reference (numbers, yes/no, conditions). An answer that states a
   wrong fact loses to one that does not.
2. It answers the actual question asked.
3. It adds no claims that are not supported by the reference or the question.

Do NOT prefer an answer because it is shown first or second: the order is random.
Do NOT prefer an answer because it is longer or more detailed. Extra detail counts only if it is
correct and relevant; padding is neutral.
If both answers are equally correct and complete, answer "tie".

Examples:
- Reference "30 days." A: "You have 30 days from delivery." B: "Returns are accepted within 30
  days of delivery, provided the item is unused and in its original packaging." -> tie (both
  correct; B's extra conditions are correct but not asked for).
- Reference "No." A: "Yes, it is covered." B: "No, accidental damage is not covered." -> B.""",
)


def ask(base_url: str, model: str, system: str, question: str) -> str:
    import httpx

    r = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": f"{system}\n\nContext:\n{CONTEXT}"},
                {"role": "user", "content": question},
            ],
        },
        timeout=300,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or ""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:1234/v1")
    ap.add_argument("--model", default="qwen/qwen3.5-9b")
    ap.add_argument("--judge-model", default=None, help="defaults to --model")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    judge_model = args.judge_model or args.model
    out_dir = ROOT / "research" / "experiments" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = Dataset.from_jsonl(ROOT / "research" / "evals" / "acme_qa.jsonl")
    examples = ds.examples[: args.limit]

    cache_path = out_dir / "e1_answers.json"
    answers: dict[str, dict[str, str]] = (
        json.loads(cache_path.read_text()) if cache_path.exists() else {}
    )
    t0 = time.time()
    for ex in examples:
        q = ex.input["question"]
        if q not in answers:
            answers[q] = {
                "brief": ask(args.base_url, args.model, BRIEF, q),
                "detailed": ask(args.base_url, args.model, DETAILED, q),
            }
            cache_path.write_text(json.dumps(answers, indent=1))
    gen_s = time.time() - t0

    results: dict[str, Any] = {
        "dataset": f"evals/acme_qa.jsonl@{ds.version}",
        "n_questions": len(examples),
        "answer_model": args.model,
        "judge_model": judge_model,
        "endpoint": "LM Studio (local)" if "1234" in args.base_url else args.base_url,
        "machine": f"{platform.machine()} {platform.system()}",
        "generation_seconds": round(gen_s, 1),
        "rubrics": {},
    }
    model = OpenAICompatibleJudge(judge_model, base_url=args.base_url, api_key="")
    for tag, rubric in (("v0", RUBRICS["correctness"]), ("v1", RUBRIC_V1)):
        judge = LLMJudge(model, rubric)
        pairs = []
        t1 = time.time()
        for ex in examples:
            a = answers[ex.input["question"]]
            # a = brief, b = detailed; pairwise_detail runs both orders.
            res = judge.pairwise_detail(Example(ex.input, ex.expected), a["brief"], a["detailed"])
            pairs.append(
                {
                    "question": ex.input["question"],
                    "first": res.first,
                    "second": res.second,
                    "flipped": res.flipped,
                    "verdict": res.verdict,
                    "error": res.error,
                }
            )
        rate, n_decided = flip_rate(pairs)
        decided = [
            p for p in pairs if p["first"] not in (None, "tie") and p["second"] not in (None, "tie")
        ]
        # "first" = verdict with brief shown first; "second" = verdict with detailed shown first.
        first_shown_wins = sum((p["first"] == "a") + (p["second"] == "b") for p in decided)
        consistent = [p for p in decided if not p["flipped"]]
        results["rubrics"][tag] = {
            "rubric": f"{rubric.name}@{rubric.version}",
            "flip_rate": rate,
            "n_pairs": len(pairs),
            "n_pairs_decided": n_decided,
            "n_ties_or_failures": len(pairs) - n_decided,
            "first_shown_win_share": first_shown_wins / (2 * len(decided)) if decided else None,
            "detailed_win_share_when_consistent": (
                sum(p["verdict"] == "b" for p in consistent) / len(consistent)
                if consistent
                else None
            ),
            "judge_seconds": round(time.time() - t1, 1),
            "pairs": pairs,
        }
        print(tag, rubric.version, "flip rate", rate, "decided", n_decided, flush=True)
    (out_dir / "e1_flip_rate.json").write_text(json.dumps(results, indent=1))
    print(
        json.dumps(
            {
                k: {kk: vv for kk, vv in v.items() if kk != "pairs"}
                for k, v in results["rubrics"].items()
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
