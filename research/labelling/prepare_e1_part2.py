"""Prepare a second batch of 100 outputs for E1, with answers that are sometimes wrong.

Why: in the first batch (`prepare_e1_queue.py`) the model answered with the policy text in front of
it, so almost every answer was correct (94 of the first 99 human labels were 5). Cohen's kappa is
close to meaningless when nearly every label is the same, whatever the judge does. A fair test of the
judge needs answers of mixed quality. This batch has:

* 70 **closed-book** answers: the same model answers the same questions *without* the policy text,
  so it has to guess. Natural mistakes, which is what a judge meets in real use.
* 30 **injected-defect** answers, made by rule from the first batch's answers: 10 with a changed
  fact (a number or a yes/no flipped), 10 that answer a different question, 5 vague non-answers
  and 5 refusals. Obvious defects, labelled as such in ``items_part2.json`` (never shown to you).

Report the two kinds separately as well as together: the injected ones are easy for a judge and
must not carry the headline number alone.

    python research/labelling/prepare_e1_part2.py --url http://localhost:8780 --home ~/tirkit-labelling

Needs a local OpenAI-compatible model for the closed-book answers (default: LM Studio on
http://localhost:1234/v1, model qwen/qwen3.5-9b). Answers are cached in
``research/experiments/results/e1_closed_book_answers.json`` so the batch can be rebuilt without it.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import httpx
from prepare_e1_queue import GUIDE, ROOT, connect, make_queue, send_traces  # type: ignore[import-not-found]

CACHE = ROOT / "research" / "experiments" / "results" / "e1_closed_book_answers.json"
QUEUE_NAME = "E1 judge labels (part 2)"
PERSONA = "You are a customer support agent for Acme Home, a company that sells robot vacuums."
PROMPTS = {
    "brief": "Answer the customer's question in one short sentence.",
    "detailed": "Answer the customer's question thoroughly in 3 to 5 sentences.",
}
N_CLOSED_BOOK, N_WRONG_FACT, N_OFF_TOPIC, N_VAGUE, N_REFUSAL = 70, 10, 10, 5, 5
VAGUE = "Please contact our customer support team for more information about this."
REFUSAL = "I'm sorry, but I can't help with that request."


def ask(base_url: str, model: str, system: str, question: str) -> str:
    r = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": f"{PERSONA} {system}"},
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


def closed_book(questions: list[str], base_url: str, model: str) -> dict[str, dict[str, str]]:
    cache: dict[str, dict[str, str]] = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for i, q in enumerate(questions):
        have = cache.setdefault(q, {})
        for variant, instruction in PROMPTS.items():
            if not have.get(variant):
                have[variant] = ask(base_url, model, instruction, q)
        if i % 10 == 9:
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
            print(f"  generated {i + 1}/{len(questions)} questions", flush=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return cache


def wrong_fact(answer: str, reference: str) -> str | None:
    """Change one fact the reference also states: a number, or a leading yes/no. None if neither."""
    m = re.search(r"\b(\d+)\b", answer)
    if m and re.search(rf"\b{m.group(1)}\b", reference):
        n = int(m.group(1))
        return answer[: m.start()] + str(n * 2 if n <= 30 else n // 2) + answer[m.end() :]
    for a, b in (("Yes", "No"), ("No", "Yes")):
        if answer.startswith(a) and reference.startswith(a):
            return b + answer[len(a) :]
    return None


def injected(part1: list[dict], rng: random.Random) -> list[dict]:
    pool = part1[:]
    rng.shuffle(pool)
    out: list[dict] = []
    for it in pool:
        if sum(o["kind"] == "wrong_fact" for o in out) >= N_WRONG_FACT:
            break
        changed = wrong_fact(it["answer"], it["reference"])
        if changed and changed != it["answer"]:
            out.append({**it, "answer": changed, "kind": "wrong_fact", "derived_from": it["item"]})
    rest = [it for it in pool if it["item"] not in {o["derived_from"] for o in out}]
    for it in rest[:N_OFF_TOPIC]:
        others = [o for o in pool if o["question"] != it["question"]]
        other = rng.choice(others)
        out.append(
            {**it, "answer": other["answer"], "kind": "off_topic", "derived_from": it["item"]}
        )
    for it in rest[N_OFF_TOPIC : N_OFF_TOPIC + N_VAGUE]:
        out.append({**it, "answer": VAGUE, "kind": "vague", "derived_from": it["item"]})
    start = N_OFF_TOPIC + N_VAGUE
    for it in rest[start : start + N_REFUSAL]:
        out.append({**it, "answer": REFUSAL, "kind": "refusal", "derived_from": it["item"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8780")
    ap.add_argument("--home", default="~/tirkit-labelling")
    ap.add_argument("--llm-base-url", default="http://localhost:1234/v1")
    ap.add_argument("--llm-model", default="qwen/qwen3.5-9b")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    home = Path(args.home).expanduser()
    url = args.url.rstrip("/")
    part1_map = json.loads((home / "items.json").read_text())
    part1 = sorted(part1_map.values(), key=lambda it: it["item"])
    questions = list(dict.fromkeys(it["question"] for it in part1))
    reference = {it["question"]: it["reference"] for it in part1}
    rng = random.Random(args.seed)

    print("closed-book answers (the model has no policy text):")
    answers = closed_book(questions, args.llm_base_url, args.llm_model)
    candidates = [
        {
            "question": q,
            "reference": reference[q],
            "answer": answers[q][v],
            "kind": "closed_book",
            "variant": v,
        }
        for q in questions
        for v in PROMPTS
    ]
    rng.shuffle(candidates)
    chosen = candidates[:N_CLOSED_BOOK]
    items = chosen + injected(part1, rng)
    kinds = {k: sum(i["kind"] == k for i in items) for k in sorted({i["kind"] for i in items})}
    if len(items) != 100:
        raise SystemExit(f"expected 100 items, built {len(items)}: {kinds}")
    print(f"batch: {kinds}")

    c, pid = connect(url, home)
    try:
        if any(q.get("name") == QUEUE_NAME for q in c.get(f"/api/projects/{pid}/queues").json()):
            print(f"the queue {QUEUE_NAME!r} already exists; nothing to do")
            return 0
        exclude = set(part1_map)
        mapping = send_traces(c, pid, url, items, exclude=exclude)
        (home / "items_part2.json").write_text(json.dumps(mapping, indent=1))
        print(f"queue {QUEUE_NAME!r}: {make_queue(c, pid, QUEUE_NAME, list(mapping), args.seed)}")
    finally:
        c.close()
    print(f"done. Label the new queue in {url} (guide: {GUIDE.relative_to(ROOT)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
