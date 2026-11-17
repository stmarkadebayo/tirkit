"""Experiment E2: does the CI gate catch real regressions without false alarms? (SPEC §8–9)

20 deliberately worse prompt variants (dropped context, truncated context, wrong format, harmful
instructions) and 20 neutral variants (rewordings with the same meaning) of a baseline support
prompt are each run over the 50 questions in research/evals/acme_qa.jsonl. The gate (SPEC §6.3) compares
every variant with the baseline on `key_fact_recall`. A run reports caught/20 and false
alarms/20. Seeds change the model's sampling (temperature 0.3, provider seed) and the bootstrap.

    python research/experiments/e2_seeded_regressions.py --seed 0 [--base-url ... --model ...]

Results are cached per (seed, variant) in research/experiments/results/e2/ so runs can resume.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from rag_demo import DOCS  # noqa: E402

from tirkit.datasets import Dataset, Example  # noqa: E402
from tirkit.runner import RunResult, gate  # noqa: E402

STOP = set(
    "a an the is are was were be to of in on for and or with by at from it its this that your you "
    "i my me we our yes no not do does can".split()
)


def key_terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def key_fact_recall(example: Example, output: str) -> float:
    """Share of the reference answer's key terms (numbers and content words) in the output.
    Yes/no references also require the right polarity at the start of the answer."""
    ref = str(example.expected)
    terms = key_terms(ref)
    out = output.lower()
    got = key_terms(output)
    score = len(terms & got) / len(terms) if terms else 1.0
    head = ref.strip().lower().split(",")[0].split(".")[0]
    if head in {"yes", "no"}:
        start = out.strip()[:40]
        right = re.search(rf"\b{head}\b", start) is not None
        wrong = re.search(rf"\b{'no' if head == 'yes' else 'yes'}\b", start) is not None
        score = score if right and not wrong else min(score, 0.5) if not wrong else 0.0
    return score


def context(docs: dict[str, str] | None = None, limit: int | None = None) -> str:
    text = "\n".join(f"[{k}] {v}" for k, v in (docs or DOCS).items())
    return text[:limit] if limit else text


BASE_SYSTEM = (
    "You are Acme's customer support assistant. Answer the customer's question using only the "
    "context below. Be accurate and brief: one or two sentences.\n\nContext:\n{context}"
)


def drop(doc: str) -> dict[str, str]:
    return {k: v for k, v in DOCS.items() if k != doc}


OTHER_SHOP = {
    "returns": "Returns are accepted within 14 days. Refunds are issued as store credit only.",
    "shipping": "Standard shipping takes 7 to 10 business days. Express costs 25 dollars.",
    "warranty": "Robots carry a 1 year warranty that covers accidental damage.",
}

# (name, system template, context, extra params)
REGRESSIONS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("drop-returns-doc", BASE_SYSTEM, context(drop("returns")), {}),
    ("drop-shipping-doc", BASE_SYSTEM, context(drop("shipping")), {}),
    ("drop-warranty-doc", BASE_SYSTEM, context(drop("warranty")), {}),
    ("drop-battery-doc", BASE_SYSTEM, context(drop("battery")), {}),
    ("drop-support-doc", BASE_SYSTEM, context(drop("support")), {}),
    ("drop-app-doc", BASE_SYSTEM, context(drop("app")), {}),
    ("truncate-context-50pct", BASE_SYSTEM, context(limit=len(context()) // 2), {}),
    ("truncate-context-20pct", BASE_SYSTEM, context(limit=len(context()) // 5), {}),
    ("no-context", BASE_SYSTEM, "(no documents available)", {}),
    ("wrong-shop-context", BASE_SYSTEM, context(OTHER_SHOP), {}),
    ("answer-in-french", BASE_SYSTEM + "\n\nAlways answer in French.", context(), {}),
    ("three-words-max", BASE_SYSTEM + "\n\nAnswer in at most three words.", context(), {}),
    (
        "no-numbers",
        BASE_SYSTEM + "\n\nNever include numbers or digits in your answer.",
        context(),
        {},
    ),
    (
        "deflect-to-support",
        BASE_SYSTEM + "\n\nDo not answer; tell the customer to contact support.",
        context(),
        {},
    ),
    (
        "pirate-persona",
        "You are a pirate. Answer in pirate speak and invent details freely.\n\nContext:\n{context}",
        context(),
        {},
    ),
    (
        "json-envelope-only",
        BASE_SYSTEM + '\n\nReply only with {{"status": "received"}}.',
        context(),
        {},
    ),
    (
        "drop-use-only-context",
        "You are a helpful assistant for an online shop. Answer from general knowledge of typical retail policies.\n\nContext:\n{context}",
        context(),
        {},
    ),
    ("high-temperature", BASE_SYSTEM, context(), {"temperature": 1.8, "top_p": 1.0}),
    ("max-tokens-4", BASE_SYSTEM, context(), {"max_tokens": 4}),
    (
        "question-rewritten",
        BASE_SYSTEM + "\n\nFirst restate the question in detail, then stop without answering.",
        context(),
        {},
    ),
]

NEUTRALS: list[tuple[str, str, str, dict[str, Any]]] = [
    (f"neutral-{i:02d}", system, ctx, {})
    for i, (system, ctx) in enumerate(
        [
            (
                "You are the customer support assistant for Acme. Using only the context, answer the customer's question accurately and briefly, in one or two sentences.\n\nContext:\n{context}",
                context(),
            ),
            (
                "Acme support assistant. Use only the context below to answer. Keep it accurate and short (one or two sentences).\n\nContext:\n{context}",
                context(),
            ),
            (
                "Answer the customer's question for Acme support. Rely only on the context. One or two accurate sentences.\n\nContext:\n{context}",
                context(),
            ),
            (BASE_SYSTEM, context(dict(reversed(list(DOCS.items()))))),
            (BASE_SYSTEM, context(dict(sorted(DOCS.items())))),
            (
                "You are Acme's friendly customer support assistant. Answer the customer's question using only the context below. Be accurate and brief: one or two sentences.\n\nContext:\n{context}",
                context(),
            ),
            (BASE_SYSTEM.replace("Be accurate and brief", "Be precise and concise"), context()),
            (BASE_SYSTEM.replace("customer's question", "question"), context()),
            (
                "Context:\n{context}\n\nYou are Acme's customer support assistant. Answer the customer's question using only the context above. Be accurate and brief: one or two sentences.",
                context(),
            ),
            (BASE_SYSTEM + "\n\nIf the context does not say, say you don't know.", context()),
            (BASE_SYSTEM + "\n\nWrite in plain English.", context()),
            (BASE_SYSTEM.replace("one or two sentences", "one to two sentences"), context()),
            (BASE_SYSTEM.replace("Acme's", "the Acme"), context()),
            (
                "System: Acme customer support.\nRules: use only the context; be accurate; answer in one or two sentences.\n\nContext:\n{context}",
                context(),
            ),
            (BASE_SYSTEM + "\n\nBe polite.", context()),
            (BASE_SYSTEM.replace("\n\nContext:", "\n\nKnowledge base:"), context()),
            (BASE_SYSTEM, "\n\n".join(f"[{k}] {v}" for k, v in DOCS.items())),
            (BASE_SYSTEM + "\n\nAnswer directly, without preamble.", context()),
            (
                BASE_SYSTEM.replace(
                    "using only the context below", "based only on the context below"
                ),
                context(),
            ),
            (
                "You help Acme's customers. Answer using only the context below, accurately, in one or two sentences.\n\nContext:\n{context}",
                context(),
            ),
        ]
    )
]


def generate(
    base_url: str,
    model: str,
    system: str,
    ctx: str,
    question: str,
    params: dict[str, Any],
    seed: int,
) -> str:
    import httpx

    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 160,
        "seed": seed,
        **params,
        "messages": [
            {"role": "system", "content": system.replace("{context}", ctx)},
            {"role": "user", "content": question},
        ],
    }
    for attempt in range(3):
        try:
            r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", json=body, timeout=300)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            return text.split("</think>", 1)[-1].strip()
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(2)
    return ""


def run_variant(
    args: argparse.Namespace, ds: Dataset, name: str, system: str, ctx: str, params: dict[str, Any]
) -> RunResult:
    cache = ROOT / "research" / "experiments" / "results" / "e2" / f"seed{args.seed}" / f"{name}.json"
    if cache.exists():
        return RunResult.from_json(json.loads(cache.read_text()))
    with ThreadPoolExecutor(args.concurrency) as pool:
        outputs = list(
            pool.map(
                lambda e: generate(
                    args.base_url, args.model, system, ctx, e.input["question"], params, args.seed
                ),
                ds.examples,
            )
        )
    scores = [key_fact_recall(e, o) for e, o in zip(ds.examples, outputs, strict=True)]
    res = RunResult(
        ds.name,
        ds.version,
        outputs=outputs,
        scores={"key_fact_recall": scores},
        name=name,
        scorer_info={"key_fact_recall": {"version": "1", "higher_is_better": True}},
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(res.to_json()))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--base-url", default="http://localhost:1234/v1")
    ap.add_argument("--model", default="qwen/qwen3.5-9b")
    ap.add_argument("--max-drop", type=float, default=0.0)
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()
    ds = Dataset.from_jsonl(ROOT / "research" / "evals" / "acme_qa.jsonl")
    t0 = time.time()
    base = run_variant(args, ds, "baseline", BASE_SYSTEM, context(), {})
    rows = []
    for group, variants in (("regression", REGRESSIONS), ("neutral", NEUTRALS)):
        for name, system, ctx, params in variants:
            res = run_variant(args, ds, name, system, ctx, params)
            passed = gate(base, res, "key_fact_recall", max_drop=args.max_drop, seed=args.seed)
            mean_b = sum(base.scores["key_fact_recall"]) / len(ds)
            mean_v = sum(s or 0 for s in res.scores["key_fact_recall"]) / len(ds)
            rows.append(
                {
                    "variant": name,
                    "group": group,
                    "gate_passed": passed,
                    "baseline_mean": mean_b,
                    "variant_mean": mean_v,
                }
            )
            print(
                f"{group:10s} {name:28s} base {mean_b:.3f} var {mean_v:.3f} gate {'pass' if passed else 'FAIL'}",
                flush=True,
            )
    caught = sum(not r["gate_passed"] for r in rows if r["group"] == "regression")
    false_alarms = sum(not r["gate_passed"] for r in rows if r["group"] == "neutral")
    summary = {
        "seed": args.seed,
        "model": args.model,
        "dataset": f"evals/acme_qa.jsonl@{ds.version}",
        "scorer": "key_fact_recall",
        "max_drop": args.max_drop,
        "caught": caught,
        "n_regressions": len(REGRESSIONS),
        "false_alarms": false_alarms,
        "n_neutral": len(NEUTRALS),
        "seconds": round(time.time() - t0, 1),
        "machine": f"{platform.machine()} {platform.system()}",
        "rows": rows,
    }
    out = ROOT / "research" / "experiments" / "results" / "e2" / f"summary_seed{args.seed}.json"
    out.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1))


if __name__ == "__main__":
    main()
