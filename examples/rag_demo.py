"""Demo app: a small RAG question-answering agent that produces realistic multi-span traces.

Each question becomes a trace: agent → retrieval → (optional tool call) → LLM call. With
``LLM_BASE_URL``/``LLM_API_KEY``/``LLM_MODEL`` set (any OpenAI-compatible endpoint, including a
local Ollama), the LLM step calls a real model through the instrumented OpenAI client. Without
them it falls back to a deterministic extractive "model" so the demo runs offline.

Run: python examples/rag_demo.py [--db demo.db] [--questions 40]
"""

from __future__ import annotations

import argparse
import os
import random
import re
from typing import Any

import tirkit
from tirkit.datasets import from_traces

DOCS = {
    "returns": "Acme accepts returns within 30 days of delivery. Items must be unused and in the "
    "original packaging. Refunds go back to the original payment method within 5 business days.",
    "shipping": "Standard shipping takes 3 to 5 business days. Express shipping takes 1 to 2 "
    "business days and costs 12 dollars. Orders over 50 dollars ship free with standard shipping.",
    "warranty": "Every Acme robot vacuum has a 2 year limited warranty covering motor and battery "
    "defects. Accidental damage is not covered.",
    "battery": "The Acme V2 battery lasts about 90 minutes per charge and fully recharges in 3 "
    "hours. Replacement batteries cost 39 dollars.",
    "support": "Support is available by chat from 8am to 8pm Eastern time, Monday to Saturday. "
    "Phone support is reserved for warranty claims.",
    "app": "The Acme Home app lets you schedule cleanings, set no-go zones and check battery "
    "level. It works on iOS 16 and Android 12 or newer.",
}

ORDERS = {"A1001": "shipped", "A1002": "processing", "A1003": "delivered"}

QUESTIONS = [
    "How long do I have to return an item?",
    "how long do I have to return an  item?",  # duplicate after lowercasing and whitespace
    "How long does standard shipping take?",
    "How much does express shipping cost?",
    "Is accidental damage covered by the warranty?",
    "How long does the V2 battery last on one charge?",
    "How long does the battery take to recharge?",
    "When is chat support available?",
    "Does the app work on Android 11?",
    "What is the status of order A1002?",
    "Where is my order A1001? My email is jane.doe@example.com",
    "Can you call me at +1 415 555 0134 about order A1003?",
    "My card 4111 1111 1111 1111 was charged twice, what do I do?",
    "Do orders over 50 dollars ship free?",
    "How much is a replacement battery?",
    "Can I set no-go zones in the app?",
    "What does the warranty cover?",
    "How fast are refunds processed after a return?",
    "Is phone support available for general questions?",
    "Can I schedule cleanings from my phone?",
]

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


@tirkit.trace(kind="retrieval")
def retrieve(question: str, k: int = 2) -> list[dict[str, str]]:
    q = words(question)
    ranked = sorted(DOCS.items(), key=lambda kv: -len(q & words(kv[1])))
    return [{"id": doc_id, "text": text} for doc_id, text in ranked[:k]]


@tirkit.trace(kind="tool")
def lookup_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": ORDERS.get(order_id, "not found")}


def extractive_model(question: str, context: list[dict[str, str]], facts: list[str]) -> str:
    """Offline stand-in for an LLM: pick the context sentence that best overlaps the question."""
    q = words(question)
    sentences = [s.strip() for d in context for s in d["text"].split(".") if s.strip()]
    best = max(sentences, key=lambda s: len(q & words(s)), default="I don't know.")
    return " ".join([*facts, best + "."])


def call_llm(question: str, context: list[dict[str, str]], facts: list[str]) -> str:
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL") or os.environ.get("JUDGE_MODEL")
    prompt = (
        "Answer the customer's question using only the context. Be brief.\n\n"
        + "\n".join(f"[{d['id']}] {d['text']}" for d in context)
        + ("\nFacts: " + "; ".join(facts) if facts else "")
        + f"\n\nQuestion: {question}"
    )
    if base_url and model:
        try:
            from openai import OpenAI
        except ImportError:
            pass
        else:
            client = OpenAI(base_url=base_url, api_key=os.environ.get("LLM_API_KEY") or "none")
            reply = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], temperature=0
            )
            return reply.choices[0].message.content or ""
    with tirkit.span("llm", kind="llm", model="demo-extractive", input={"prompt": prompt}) as s:
        answer = extractive_model(question, context, facts)
        s.output = answer
        s.set_usage(input_tokens=len(prompt.split()), output_tokens=len(answer.split()))
    return answer


@tirkit.trace(kind="agent", name="support-agent")
def answer(question: str) -> str:
    context = retrieve(question)
    facts = []
    for order_id in re.findall(r"\bA\d{4}\b", question):
        info = lookup_order(order_id)
        facts.append(f"Order {info['order_id']} is {info['status']}.")
    return call_llm(question, context, facts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    ap.add_argument("--questions", type=int, default=len(QUESTIONS))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    server_mode = bool(os.environ.get("TIRKIT_HOST"))
    client = tirkit.init() if server_mode else tirkit.init(path=args.db)
    tirkit.instrument()
    rng = random.Random(args.seed)
    questions = [QUESTIONS[i % len(QUESTIONS)] for i in range(args.questions)]
    for i, q in enumerate(questions):
        with tirkit.trace_context(
            session_id=f"session-{i // 4}", user_id=f"user-{rng.randint(1, 5)}", release="demo-v1"
        ):
            answer(q)
    tirkit.flush()
    if server_mode:
        print(f"sent {len(questions)} traces to {os.environ['TIRKIT_HOST']}")
        client.shutdown()
        return

    store = client.store
    roots = store.query_spans({"name": "support-agent"}, limit=10_000)
    ds = from_traces(roots, name="support-faq")
    version = store.save_dataset(ds, description="Questions mined from demo traces, PII redacted")
    print(f"traced {len(questions)} questions -> {store.count_traces()} traces")
    print(f"dataset support-faq@{version}: {len(ds)} examples after dedupe and redaction")
    sample: Any = next((e for e in ds if "[EMAIL]" in str(e.input)), None)
    if sample:
        print("redacted example:", sample.input)
    client.shutdown()


if __name__ == "__main__":
    main()
