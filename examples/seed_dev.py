"""Fill a local development server with complete, SYNTHETIC demo data so every screen has content.

    tirkit serve --port 8000                       # in one terminal
    python examples/seed_dev.py --url http://127.0.0.1:8000     # or: make dev-seed

DEVELOPMENT ONLY. Everything this script creates is made up: the questions, answers, model
names, latencies, costs, labels, judge scores, experiment results and reliability figures are
generated from a fixed seed by small scripts, with no network and no LLM (the "judge" is a
scripted stand-in, never a real model). Never present these numbers as results, and never copy
them into the README. The login below is a fixed test account for a local instance; do not use it
on a server anyone else can reach.

It builds, in order (each step is skipped when its data already exists, so running it twice is
safe, and the content is identical on every run apart from timestamps, which are relative to now):

1. ~140 support-bot traces over six days (sessions, users, tool calls, PII, errors, user feedback)
   plus a vision call, a speech call and a failing call;
2. prompts with versions and labels (``support-answer`` at v1 to v3, ``triage-classifier``), linked
   to traces through ``prompt_version``;
3. the dataset ``support-faq``, mined from traces (v1), extended (v2) and curated (v3);
4. experiments on that dataset: a baseline, an improved prompt and a regressed one (whose gate
   fails), each scored by rules (contains_expected, wer) and a scripted correctness judge;
5. an annotation queue with 60 human labels and 25 items left to label, then two judge-reliability
   reports (two rubric versions) computed from those labels with the scripted judge, including
   position-swap pairs;
6. online evaluators with scored traces, and alert rules (one of them firing).

Steps 5 and 6 write through the store layer because the HTTP API only runs a real model for them.
Pass ``--db`` (default: ``$TIRKIT_DATABASE_URL`` or ``tirkit.db`` in the current directory,
which is where ``tirkit serve`` keeps it); without a database those parts are skipped.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import math
import os
import random
import re
import struct
import sys
import time
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEV_EMAIL = "admin@tirkit.test"
DEV_PASSWORD = "tirkit-dev-only-password"
SEED = 20240611
SYNTHETIC = "synthetic dev data (scripted, no model was called)"
JUDGE_MODEL = "scripted-judge (synthetic)"

DATASET = "support-faq"
QUEUE_NAME = "Support answers: weekly review"
N_LABELLED = 60
N_UNLABELLED = 25
KEY_NAME = "seed-dev"
DAY = 86400.0

# -- the made-up knowledge base ------------------------------------------------------------------

FACTS: dict[str, list[str]] = {
    "returns": [
        "Acme accepts returns within 30 days of delivery.",
        "Items must be unused and in the original packaging.",
        "Refunds go back to the original payment method within 5 business days.",
        "Return shipping is free with the prepaid label in your confirmation email.",
    ],
    "shipping": [
        "Standard shipping takes 3 to 5 business days.",
        "Express shipping takes 1 to 2 business days and costs 12 dollars.",
        "Orders over 50 dollars ship free with standard shipping.",
        "Acme ships to the United States and Canada only.",
    ],
    "warranty": [
        "Every Acme robot vacuum has a 2 year limited warranty covering motor and battery defects.",
        "Accidental damage is not covered by the warranty.",
        "A warranty claim needs the order number or proof of purchase.",
        "The warranty does not transfer to a new owner.",
    ],
    "battery": [
        "The Acme V2 battery lasts about 90 minutes per charge.",
        "The battery fully recharges in 3 hours.",
        "Replacement batteries cost 39 dollars.",
        "Batteries keep most of their capacity for about 500 charge cycles.",
    ],
    "support": [
        "Chat support is available from 8am to 8pm Eastern time, Monday to Saturday.",
        "Phone support is reserved for warranty claims.",
        "Email questions are answered within 1 business day.",
        "Support is closed on public holidays.",
    ],
    "app": [
        "The Acme Home app lets you schedule cleanings from your phone.",
        "The app works on iOS 16 and Android 12 or newer.",
        "You can draw no-go zones on the map in the app.",
        "Firmware updates install over Wi-Fi from the app.",
    ],
    "maintenance": [
        "Clean the main brush once a week to remove hair.",
        "Replace the filter every 3 months.",
        "Empty the dust bin after every cleaning run.",
        "Replace the side brush every 6 months.",
    ],
    "setup": [
        "The robot connects to 2.4 GHz Wi-Fi networks only.",
        "Charge the robot for 3 hours before the first cleaning.",
        "Place the dock against a wall with 1 metre of free space on each side.",
        "Hold the reset button for 10 seconds to restore factory settings.",
    ],
}

# Two phrasings per fact, in the order of FACTS.
PHRASINGS: dict[str, list[tuple[str, str]]] = {
    "returns": [
        ("How long do I have to return an item?", "What is the return window after delivery?"),
        (
            "Can I return something I already opened?",
            "Does the box have to be intact for a return?",
        ),
        ("When will my refund show up?", "How fast are refunds processed after a return?"),
        ("Do I pay to send a return back?", "Who covers the cost of return shipping?"),
    ],
    "shipping": [
        ("How long does standard shipping take?", "When should a normal delivery arrive?"),
        (
            "How much does express shipping cost?",
            "How quick is express delivery and what is the fee?",
        ),
        ("Is there free shipping on bigger orders?", "Do orders over 50 dollars ship free?"),
        ("Do you deliver outside the US?", "Can you ship to Canada or Europe?"),
    ],
    "warranty": [
        ("How long is the warranty on the vacuum?", "What does the 2 year warranty cover?"),
        ("Is accidental damage covered?", "I dropped it down the stairs, will the warranty pay?"),
        ("What do I need to start a warranty claim?", "Which documents does a claim require?"),
        (
            "Can I pass the warranty to a buyer if I sell it?",
            "Does the warranty move with the robot?",
        ),
    ],
    "battery": [
        ("How long does the V2 battery last on one charge?", "What is the runtime per charge?"),
        (
            "How long does the battery take to recharge?",
            "How many hours until it is fully charged?",
        ),
        ("How much is a replacement battery?", "What does a new battery cost?"),
        ("How long before the battery wears out?", "How many charge cycles does the battery last?"),
    ],
    "support": [
        ("When is chat support available?", "What are your support hours?"),
        (
            "Can I call someone about a problem?",
            "Is phone support available for general questions?",
        ),
        ("How quickly do you reply to email?", "What is the email response time?"),
        ("Is support open on holidays?", "Can I reach anyone on a public holiday?"),
    ],
    "app": [
        ("Can I schedule cleanings from my phone?", "How do I set a cleaning schedule?"),
        ("Does the app work on Android 11?", "Which phone versions does the app support?"),
        ("Can I set no-go zones in the app?", "How do I keep the robot out of a room?"),
        ("How do I update the firmware?", "Does the robot update itself over Wi-Fi?"),
    ],
    "maintenance": [
        (
            "How often should I clean the main brush?",
            "Hair keeps wrapping around the brush, how often to clean it?",
        ),
        ("How often does the filter need replacing?", "When should I swap the filter?"),
        ("Should I empty the dust bin every time?", "How often do I empty the bin?"),
        ("How often should the side brush be replaced?", "When does the side brush wear out?"),
    ],
    "setup": [
        (
            "Why will it not connect to my 5 GHz network?",
            "Which Wi-Fi bands does the robot support?",
        ),
        ("Should I charge it before the first run?", "How long do I charge a brand new robot?"),
        ("Where should I put the charging dock?", "How much space does the dock need?"),
        (
            "How do I factory reset the robot?",
            "What does the reset button do and how long do I hold it?",
        ),
    ],
}

ORDERS = {"A1001": "shipped", "A1002": "processing", "A1003": "delivered", "A1004": "cancelled"}
ORDER_QUESTIONS = [
    "What is the status of order {o}?",
    "Where is my order {o}?",
    "Can you check order {o} for me?",
    "Has order {o} left the warehouse yet?",
    "I want an update on order {o}, please.",
]
PII_QUESTIONS = [
    ("Where is my order A1001? My email is jane.doe@example.com", "A1001"),
    ("Can you call me at +1 415 555 0134 about order A1003?", "A1003"),
    ("My card 4111 1111 1111 1111 was charged twice on order A1002, what should I do?", "A1002"),
]
DECORATIONS = [
    "Hi there, {q} Thanks!",
    "Quick question before I buy a second one: {q}",
    "{q} I could not find it on the website.",
    "Hello, my robot arrived last week. {q}",
    "{q} It is for my mother who is not good with tech.",
    "Sorry to bother you again. {q}",
]
FILLER = [
    "I hope that helps, and please let us know if there is anything else you would like to know "
    "about your Acme robot.",
    "Thank you for being an Acme customer, we really appreciate your patience and your support "
    "and we are always happy to assist you further whenever you need.",
]
GENERIC = [
    "You can find more details on our help page.",
    "Please contact support for more information.",
    "That depends on your situation.",
]

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def unit(*parts: object) -> float:
    """A deterministic number in [0, 1) from the parts (stable across runs and machines)."""
    h = hashlib.sha256("|".join(str(p) for p in (SEED, *parts)).encode()).hexdigest()
    return int(h[:12], 16) / 16**12


def hexid(label: str, n: int) -> str:
    return hashlib.sha256(f"{SEED}:{label}".encode()).hexdigest()[:n]


def _build_questions() -> tuple[list[tuple[str, str]], dict[str, str]]:
    """(question, topic) pairs in a fixed order, and the correct answer for each question."""
    qs: list[tuple[str, str]] = []
    correct: dict[str, str] = {}
    for topic, facts in FACTS.items():
        for fact, pair in zip(facts, PHRASINGS[topic], strict=True):
            for q in pair:
                qs.append((q, topic))
                correct[q] = fact
    plain = list(qs)
    for i, (q, topic) in enumerate(plain):
        dq = DECORATIONS[i % len(DECORATIONS)].format(q=q)
        qs.append((dq, topic))
        correct[dq] = correct[q]
    orders = list(ORDERS)
    for i in range(10):
        oid = orders[i % len(orders)]
        q = ORDER_QUESTIONS[i % len(ORDER_QUESTIONS)].format(o=oid)
        if q in correct:
            q = q + " Thanks."
        qs.append((q, "orders"))
        correct[q] = f"Order {oid} is {ORDERS[oid]}."
    for q, oid in PII_QUESTIONS:
        qs.append((q, "orders"))
        correct[q] = f"Order {oid} is {ORDERS[oid]}."
    return qs, correct


QUESTIONS, CORRECT = _build_questions()
ALL_SENTENCES = [s for facts in FACTS.values() for s in facts]


def answer_for(question: str) -> str:
    """The correct answer for a question; falls back to the closest known question (the dataset
    miner redacts PII, which changes the text of a few questions)."""
    if question in CORRECT:
        return CORRECT[question]
    close = difflib.get_close_matches(question, list(CORRECT), n=1, cutoff=0.4)
    return CORRECT[close[0]] if close else ALL_SENTENCES[0]


def make_output(question: str, kind: str) -> str:
    """One of four kinds of answer to a question: correct, padded, partial or wrong."""
    right = answer_for(question)
    if kind == "correct":
        return right
    if kind == "padded":
        return f"{right} {FILLER[int(unit('filler', question) * len(FILLER))]}"
    if kind == "partial":
        ws = right.split()
        if unit("partial", question) < 0.5:
            return " ".join(ws[: max(3, len(ws) // 2)]) + "..."
        return GENERIC[int(unit("generic", question) * len(GENERIC))]
    m = re.match(r"Order (A\d{4}) is (\w+)\.", right)
    if m:
        others = [s for s in set(ORDERS.values()) if s != m.group(2)]
        return (
            f"Order {m.group(1)} is {sorted(others)[int(unit('wrong', question) * len(others))]}."
        )
    pool = [s for s in ALL_SENTENCES if s != right]
    return pool[int(unit("wrong", question) * len(pool))]


def quality(question: str, output: Any) -> int:
    """How good an answer is, 1-5, by comparing it with the correct answer (no model involved)."""
    text = output if isinstance(output, str) else ("" if output is None else str(output))
    right = answer_for(question)
    out_w, right_w = words(text), words(right)
    if out_w == right_w:
        return 5
    if " ".join(right_w) in " ".join(out_w):
        return 4
    overlap = len(set(out_w) & set(right_w)) / max(1, len(set(right_w)))
    if overlap >= 0.8 and not text.endswith("..."):
        return 2  # right words, wrong fact (such as another order status)
    if overlap >= 0.55:
        return 3
    return 2 if overlap >= 0.3 else 1


def proxy_quality(output: Any) -> int:
    """Quality guess from the answer alone (pairwise comparisons do not carry the question)."""
    text = output if isinstance(output, str) else ""
    norm = " ".join(words(text))
    if any(norm == " ".join(words(s)) for s in ALL_SENTENCES) or re.match(
        r"order a\d{4} is ", norm
    ):
        return 5
    if any(" ".join(words(s)) in norm for s in ALL_SENTENCES):
        return 4
    return 2 if len(norm.split()) <= 6 else 3


# -- scripted judge and human labels (no model) ---------------------------------------------------


@dataclass(frozen=True)
class SeedRubric:
    """Duck-typed like :class:`tirkit.judge.Rubric` (name, text, version)."""

    name: str
    text: str
    version: str


def _rubric(name: str, text: str) -> SeedRubric:
    return SeedRubric(name, text, hashlib.sha256(text.encode()).hexdigest()[:8])


RUBRIC_V1 = _rubric(
    "correctness",
    "[synthetic dev rubric, first draft] Score how correct the OUTPUT is as an answer to the "
    "INPUT, from 1 (wrong) to 5 (fully correct).",
)
RUBRIC_V2 = _rubric(
    "correctness",
    "[synthetic dev rubric, calibrated] Score how correct the OUTPUT is as an answer to the INPUT, "
    "using the EXPECTED answer as the reference.\n"
    "5: matches the reference in substance, nothing wrong added.\n"
    "4: correct but padded with filler.\n"
    "3: partly correct; a key part is missing.\n"
    "2: states the wrong fact, or too little to help.\n"
    "1: off topic or a non-answer.\n"
    "Do not reward length.",
)


def human_label(question: str, output: Any) -> int:
    """The scripted reviewer: the true quality, wrong by one point about one time in five."""
    q = quality(question, output)
    u = unit("human", question, str(output))
    if u < 0.80:
        return q
    return max(1, min(5, q + (-1 if u < 0.90 else 1)))


class ScriptedJudge:
    """A fake LLM judge. Scores come from :func:`quality` plus fixed noise, and the noise is
    smaller for the calibrated rubric. Never calls a model. Provides what the reliability code and
    the online evaluator use: ``score``, ``pairwise_detail``, ``name``, ``version``."""

    higher_is_better = True

    def __init__(self, rubric: SeedRubric, *, name: str | None = None) -> None:
        from tirkit.types import Score  # noqa: F401  (import check; used in score())

        self.rubric = rubric
        self.name = name or f"judge:{rubric.name}"
        self.version = f"{rubric.version}:scripted"
        calibrated = rubric.version == RUBRIC_V2.version
        self.exact = 0.66 if calibrated else 0.46
        self.off_by_one = 0.28 if calibrated else 0.40
        self.length_bump = 0.20 if calibrated else 0.55
        self.position_bias = 0.06 if calibrated else 0.14
        self.position_noise = 0.4 if calibrated else 0.5

    def score(self, example: Any, output: Any) -> Any:
        from tirkit.types import Score

        question = str(example.input.get("question") or example.input.get("input") or "")
        q = quality(question, output)
        u = unit("judge", self.rubric.version, question, str(output))
        if u < self.exact:
            v = q
        elif u < self.exact + self.off_by_one:
            v = q + (1 if unit("dir", self.rubric.version, question, str(output)) < 0.5 else -1)
        else:
            v = q + (2 if unit("dir", self.rubric.version, question, str(output)) < 0.5 else -2)
        n_words = len(words(output if isinstance(output, str) else ""))
        if n_words >= 14 and unit("len", self.rubric.version, question) < self.length_bump:
            v += 1  # longer answers get a better score than they deserve
        v = max(1, min(5, v))
        return Score(float(v), reason=f"scripted judgement, quality {q} (synthetic)")

    def pairwise_detail(self, example: Any, a: Any, b: Any) -> Any:
        from tirkit.judge import PairwiseResult

        def one(first: Any, second: Any, tag: str) -> str:
            d = proxy_quality(first) - proxy_quality(second)
            d += 0.03 * (len(words(str(first))) - len(words(str(second))))
            d += (
                self.position_bias
                + (unit("pos", tag, str(first), str(second)) - 0.5) * self.position_noise
            )
            return "first" if d > 0.1 else ("second" if d < -0.1 else "tie")

        r1 = one(a, b, "ab")  # A shown first
        r2 = one(b, a, "ba")  # B shown first
        first = {"first": "a", "second": "b", "tie": "tie"}[r1]
        second = {"first": "b", "second": "a", "tie": "tie"}[r2]
        decided = first != "tie" and second != "tie"
        flipped = decided and first != second
        verdict = first if first == second else "tie"
        return PairwiseResult(
            verdict, first, second, flipped, ("scripted (synthetic)", "scripted (synthetic)")
        )  # type: ignore[arg-type]


# -- synthetic traffic ----------------------------------------------------------------------------


@dataclass
class Traffic:
    idx: int
    trace_id: str
    question: str
    output: str | None
    error: bool
    user: str
    session: str
    release: str
    prompt: str
    start: float
    topic: str


def build_traffic(n: int = 140) -> list[Traffic]:
    """The support-bot traffic, in time order. Content and ids are fixed; ``start`` is a day offset
    that :func:`spans_for` turns into timestamps relative to now."""
    rng = random.Random(SEED)
    order = list(range(len(QUESTIONS)))
    rng.shuffle(order)
    errors = {7, 19, 33, 48, 52, 71, 88, 104, 121, 133, n - 3}
    out: list[Traffic] = []
    for i in range(n):
        question, topic = QUESTIONS[order[i % len(QUESTIONS)]]
        u = unit("prod", question)
        kind = (
            "correct" if u < 0.64 else "padded" if u < 0.76 else "partial" if u < 0.88 else "wrong"
        )
        is_err = i in errors
        release = "demo-v1" if i < 90 else "demo-v2"
        out.append(
            Traffic(
                idx=i,
                trace_id=hexid(f"trace:{i}", 32),
                question=question,
                output=None if is_err else make_output(question, kind),
                error=is_err,
                user=f"user-{rng.randint(1, 8)}",
                session=f"session-{i // 4}",
                release=release,
                prompt="support-answer@1" if i < 60 else "support-answer@2",
                start=i / (n - 1),
                topic=topic,
            )
        )
    return out


def _span(
    name: str,
    t: Traffic,
    key: str,
    *,
    parent: str | None,
    start: float,
    dur: float,
    kind: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "trace_id": t.trace_id,
        "span_id": hexid(f"span:{t.idx}:{key}", 16),
        "parent_id": parent,
        "start": start,
        "end": start + dur,
        "kind": kind,
        "session_id": t.session,
        "user_id": t.user,
        "release": t.release,
        "tags": ["support", "synthetic"],
        **extra,
    }


def spans_for(t: Traffic, now: float) -> list[dict[str, Any]]:
    """Agent root, retrieval, optional order lookup and the LLM call for one question."""
    span_s = 6 * DAY - 5400
    start = now - 6 * DAY + 3600 + t.start * span_s + (unit("jitter", t.idx) - 0.5) * 900
    retr = 0.06 + unit("retr", t.idx) * 0.14
    oid = re.search(r"\bA\d{4}\b", t.question)
    tool = (0.08 + unit("tool", t.idx) * 0.22) if oid else 0.0
    llm = 0.5 + unit("llm", t.idx) ** 2 * 2.6
    if t.error:
        llm = 3.0 + unit("llm", t.idx) * 0.2
    total = retr + tool + llm + 0.02
    root = _span(
        "support-agent",
        t,
        "root",
        parent=None,
        start=start,
        dur=total,
        kind="agent",
        input={"question": t.question},
        output=t.output,
        prompt_version=t.prompt,
        status="error" if t.error else "ok",
        level="error" if t.error else "default",
        error="TimeoutError: upstream timed out after 3s" if t.error else None,
    )
    rid = root["span_id"]
    docs = [{"id": t.topic, "text": " ".join(FACTS.get(t.topic, [CORRECT[t.question]]))}]
    spans = [
        root,
        _span(
            "retrieve",
            t,
            "retr",
            parent=rid,
            start=start + 0.01,
            dur=retr,
            kind="retrieval",
            input={"query": t.question},
            output=docs,
        ),
    ]
    cursor = start + 0.01 + retr
    if oid:
        spans.append(
            _span(
                "lookup_order",
                t,
                "tool",
                parent=rid,
                start=cursor,
                dur=tool,
                kind="tool",
                input={"order_id": oid.group(0)},
                output={"order_id": oid.group(0), "status": ORDERS[oid.group(0)]},
            )
        )
        cursor += tool
    n_in = 220 + int(unit("tin", t.idx) * 680)
    n_out = len(words(t.output or "")) + 8 if t.output else 0
    prompt_text = (
        "Answer the customer's question using only the context. Be brief.\n\n"
        f"[{t.topic}] {docs[0]['text']}\n\nQuestion: {t.question}"
    )
    spans.append(
        _span(
            "llm",
            t,
            "llm",
            parent=rid,
            start=cursor,
            dur=llm,
            kind="llm",
            model="gpt-4o-mini",
            model_params={"temperature": 0},
            input={"messages": [{"role": "user", "content": prompt_text}]},
            output={"role": "assistant", "content": t.output} if t.output else None,
            usage={"input_tokens": n_in, "output_tokens": n_out} if t.output else {},
            prompt_version=t.prompt,
            ttft_ms=150 + unit("ttft", t.idx) * 350,
            status="error" if t.error else "ok",
            level="error" if t.error else "default",
            error="TimeoutError: upstream timed out after 3s" if t.error else None,
        )
    )
    return spans


def wav_tone(seconds: float = 1.2, rate: int = 16000, freq: float = 440.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return buf.getvalue()


def png_square(size: int = 64) -> bytes:
    """A small valid PNG (purple square with a lighter centre)."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inner = size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
            row += bytes((190, 170, 255) if inner else (106, 76, 245))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


# -- simulated systems for the experiments --------------------------------------------------------

SYSTEMS: dict[str, dict[str, float]] = {
    # cumulative thresholds on a per-item number: correct, then padded, then partial, else wrong
    "baseline": {"correct": 0.50, "padded": 0.60, "partial": 0.76},
    "improved": {"correct": 0.68, "padded": 0.74, "partial": 0.86},
    "terse": {"correct": 0.34, "padded": 0.36, "partial": 0.62},
}


def system_output(system: str, question: str) -> str:
    """What a simulated prompt/model combination answers; items are correlated across systems
    (hard questions are hard for all) so paired comparisons have real structure."""
    th = SYSTEMS[system]
    x = 0.5 * unit("difficulty", question) + 0.5 * unit("system", system, question)
    # x is triangular on [0, 1]; stretch it back to roughly uniform
    x = 2 * x * x if x < 0.5 else 1 - 2 * (1 - x) ** 2
    if x < th["correct"]:
        kind = "correct"
    elif x < th["padded"]:
        kind = "padded"
    elif x < th["partial"]:
        kind = "partial"
    else:
        kind = "wrong"
    return make_output(question, kind)


# -- the seeder ------------------------------------------------------------------------------------


class Seeder:
    """Drives a Tirkit server through its HTTP API (and, for the judge-driven parts, its store)."""

    def __init__(self, client: httpx.Client, store: Any = None, *, log: Any = print) -> None:
        self.c = client
        self.store = store  # project-scoped SQLStore, or None
        self.log = log
        self.now = time.time()
        self.pid = ""
        self.auth: tuple[str, str] = ("", "")
        self.summary: dict[str, Any] = {}
        self.traffic = build_traffic()

    # -- plumbing ---------------------------------------------------------------------------------

    def req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    def api(self, path: str) -> str:
        return f"/api/projects/{self.pid}{path}"

    def say(self, msg: str) -> None:
        self.log(f"  {msg}")

    # -- account and key --------------------------------------------------------------------------

    def login(self) -> None:
        if self.c.get("/api/setup").json()["needs_setup"]:
            self.req(
                "POST",
                "/api/setup",
                json={
                    "email": DEV_EMAIL,
                    "password": DEV_PASSWORD,
                    "name": "Dev Admin",
                    "project_name": "Acme support bot",
                },
            )
        else:
            self.req("POST", "/api/auth/login", json={"email": DEV_EMAIL, "password": DEV_PASSWORD})
        me = self.req("GET", "/api/auth/me").json()
        self.c.headers["X-CSRF-Token"] = me["csrf"]
        self.pid = me["projects"][0]["project_id"]

    def new_key(self) -> None:
        """One API key for this run; keys from earlier runs are revoked so they do not pile up."""
        for k in self.req("GET", self.api("/keys")).json():
            if k.get("name") == KEY_NAME:
                self.req("DELETE", self.api(f"/keys/{k['public_key']}"))
        k = self.req("POST", self.api("/keys"), json={"name": KEY_NAME}).json()
        self.auth = (k["public_key"], k["secret_key"])

    def ingest(self, spans: list[dict[str, Any]]) -> None:
        for i in range(0, len(spans), 300):
            self.req("POST", "/api/v1/ingest", json={"spans": spans[i : i + 300]}, auth=self.auth)

    def has_trace(self, trace_id: str) -> bool:
        return self.c.get(self.api(f"/traces/{trace_id}")).status_code == 200

    # -- 1. traces --------------------------------------------------------------------------------

    def traces(self) -> None:
        if self.has_trace(self.traffic[0].trace_id):
            self.say("traces: already seeded, skipped")
            return
        spans = [s for t in self.traffic for s in spans_for(t, self.now)]
        self.ingest(spans)
        n_fb = 0
        for t in self.traffic:
            if t.error or unit("fb", t.idx) > 0.22:
                continue
            good = quality(t.question, t.output) >= 4
            thumbs = 1 if (good if unit("fbv", t.idx) < 0.85 else not good) else 0
            self.req(
                "POST",
                "/api/v1/scores",
                auth=self.auth,
                json={
                    "name": "user_feedback",
                    "value": thumbs,
                    "trace_id": t.trace_id,
                    "source": "user_feedback",
                    "comment": "helpful" if thumbs else "did not answer my question",
                },
            )
            n_fb += 1
        self.media_traces()
        self.say(
            f"traces: {len(self.traffic)} support traces, {n_fb} feedback scores, vision, speech"
        )

    def media_traces(self) -> None:
        def upload(data: bytes, mime: str) -> dict[str, Any]:
            sha = hashlib.sha256(data).hexdigest()
            self.req(
                "PUT",
                f"/api/v1/media/{sha}",
                content=data,
                headers={"content-type": mime},
                auth=self.auth,
            )
            return {"type": "tirkit.media", "sha256": sha, "mime": mime, "size": len(data)}

        from tirkit.media import MEDIA_TYPE

        def ref(data: bytes, mime: str) -> dict[str, Any]:
            r = upload(data, mime)
            r["type"] = MEDIA_TYPE
            return r

        base = self.now - 2 * 3600
        png = ref(png_square(), "image/png")
        tone = ref(wav_tone(), "audio/wav")
        tone2 = ref(wav_tone(1.0, freq=330), "audio/wav")

        def sp(
            trace: str, key: str, name: str, parent: str | None, off: float, dur: float, **kw: Any
        ) -> dict[str, Any]:
            return {
                "name": name,
                "trace_id": hexid(f"media:{trace}", 32),
                "span_id": hexid(f"media:{trace}:{key}", 16),
                "parent_id": hexid(f"media:{trace}:root", 16) if parent else None,
                "start": base + off,
                "end": base + off + dur,
                "release": "demo-v1",
                "tags": ["synthetic"],
                **kw,
            }

        spans = [
            sp(
                "vision",
                "root",
                "describe-photo",
                None,
                0,
                1.4,
                kind="agent",
                session_id="vision-1",
                user_id="user-7",
                input={"question": "What is in this photo?"},
                output="A purple square with a lighter square in the middle.",
            ),
            sp(
                "vision",
                "llm",
                "gpt-4o vision",
                "root",
                0.05,
                1.2,
                kind="llm",
                model="gpt-4o",
                session_id="vision-1",
                user_id="user-7",
                input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "What is in this photo?"},
                                {"type": "image_url", "image_url": {"url": png}},
                            ],
                        }
                    ]
                },
                output={
                    "role": "assistant",
                    "content": "A purple square with a lighter square in the middle.",
                },
                usage={"input_tokens": 310, "output_tokens": 14},
            ),
            sp(
                "voice",
                "root",
                "voice-turn",
                None,
                60,
                2.6,
                kind="agent",
                session_id="voice-1",
                user_id="user-3",
                input={"audio": tone},
                output="Your order shipped yesterday.",
            ),
            sp(
                "voice",
                "stt",
                "transcribe",
                "root",
                60.1,
                0.6,
                kind="speech_to_text",
                model="whisper-1",
                session_id="voice-1",
                user_id="user-3",
                input={"audio": tone},
                output={"text": "Where is my order?"},
                usage={"input_audio_seconds": 1.2},
                attributes={"real_time_factor": 0.21},
            ),
            sp(
                "voice",
                "tts",
                "speak",
                "root",
                60.8,
                1.2,
                kind="text_to_speech",
                model="tts-1",
                session_id="voice-1",
                user_id="user-3",
                input={"text": "Your order shipped yesterday."},
                output={"audio": tone2},
                usage={"input_characters": 29, "output_audio_seconds": 1.0},
            ),
            sp(
                "flaky",
                "root",
                "flaky-call",
                None,
                120,
                3.0,
                kind="llm",
                model="gpt-4o-mini",
                input="Summarise the ticket",
                status="error",
                level="error",
                error="TimeoutError: upstream timed out after 30s",
            ),
        ]
        self.ingest(spans)
        self.req(
            "POST",
            "/api/v1/scores",
            auth=self.auth,
            json={
                "name": "user_feedback",
                "value": 1,
                "trace_id": hexid("media:voice", 32),
                "source": "user_feedback",
                "comment": "helpful voice answer",
            },
        )

    # -- 2. prompts -------------------------------------------------------------------------------

    def prompts(self) -> None:
        have = {p["name"] for p in self.req("GET", self.api("/prompts")).json()}
        made = 0

        def version(
            name: str, template: Any, kind: str, labels: list[str], message: str, cfg: Any
        ) -> None:
            nonlocal made
            self.req(
                "POST",
                self.api(f"/prompts/{name}/versions"),
                json={
                    "template": template,
                    "kind": kind,
                    "labels": labels,
                    "message": message,
                    "config": cfg,
                },
            )
            made += 1

        cfg = {"model": "gpt-4o-mini", "params": {"temperature": 0}}
        if "support-answer" not in have:
            version(
                "support-answer",
                [
                    {
                        "role": "system",
                        "content": "You are a support agent for Acme robot vacuums. Answer from the context.",
                    },
                    {"role": "user", "content": "Context: {{context}}\n\nQuestion: {{question}}"},
                ],
                "chat",
                ["baseline"],
                "First version",
                cfg,
            )
            version(
                "support-answer",
                [
                    {
                        "role": "system",
                        "content": "You are a support agent for Acme robot vacuums. Answer in one sentence, using only the context. If the context does not say, tell the customer to contact support.",
                    },
                    {"role": "user", "content": "Context: {{context}}\n\nQuestion: {{question}}"},
                ],
                "chat",
                ["production"],
                "One sentence, say when unsure",
                cfg,
            )
            version(
                "support-answer",
                [
                    {"role": "system", "content": "Answer as briefly as possible."},
                    {"role": "user", "content": "{{context}}\n{{question}}"},
                ],
                "chat",
                ["staging"],
                "Terse variant under test",
                cfg,
            )
        if "triage-classifier" not in have:
            version(
                "triage-classifier",
                "Classify the customer message into one of: returns, shipping, warranty, battery, "
                "app, other. Message: {{message}}\nAnswer with the label only.",
                "text",
                ["production"],
                "Initial classifier",
                {"model": "gpt-4o-mini"},
            )
            version(
                "triage-classifier",
                "You route support tickets. Labels: returns, shipping, warranty, battery, app, "
                "other.\nMessage: {{message}}\nReply with exactly one label.",
                "text",
                ["staging"],
                "Stricter output format",
                {"model": "gpt-4o-mini"},
            )
        self.say(
            f"prompts: {made} versions created" if made else "prompts: already seeded, skipped"
        )

    # -- 3. dataset -------------------------------------------------------------------------------

    def dataset(self) -> dict[str, Any]:
        def detail() -> dict[str, Any] | None:
            r = self.c.get(self.api(f"/datasets/{DATASET}"))
            return r.json() if r.status_code == 200 else None

        d = detail()
        if d is not None and len(d["versions"]) >= 3:
            self.say(f"dataset: {DATASET} already seeded ({len(d['items'])} items), skipped")
            return d
        ok = [t for t in self.traffic if not t.error]
        first, second = [t.trace_id for t in ok[:55]], [t.trace_id for t in ok[55:95]]
        body = {"name": DATASET, "expected_from_output": True}
        self.req(
            "POST",
            self.api("/datasets/from-traces"),
            json={
                **body,
                "trace_ids": first,
                "description": "Support questions mined from the first days of production traces",
            },
        )
        self.req(
            "POST",
            self.api("/datasets/from-traces"),
            json={
                **body,
                "trace_ids": second,
                "append": True,
                "description": "Added the following days; near-duplicates removed",
            },
        )
        d = detail() or {}
        items = []
        fixed = 0
        for it in d["items"]:
            q = (it["input"] or {}).get("question", "")
            right = answer_for(q)
            if it.get("expected") != right:
                fixed += 1
            meta = {**(it.get("metadata") or {}), "reviewed": True}
            items.append({"input": it["input"], "expected": right, "metadata": meta})
        self.req(
            "PUT",
            self.api(f"/datasets/{DATASET}"),
            json={
                "items": items,
                "description": "Curated: references checked by a person",
                "note": f"Replaced {fixed} wrong production answers with the correct reference",
            },
        )
        d = detail() or {}
        self.say(f"dataset: {DATASET} {len(d['versions'])} versions, {len(d['items'])} items")
        return d

    # -- 4. experiments ---------------------------------------------------------------------------

    def experiments(self, ds_detail: dict[str, Any]) -> None:
        from tirkit.datasets import Dataset, Example
        from tirkit.runner import gate, run
        from tirkit.scorer_specs import build_scorer

        existing = {e["name"] for e in self.req("GET", self.api("/experiments")).json()}
        runs = [
            ("baseline: support-answer@1", "baseline", "support-answer@1", 5.0),
            ("improved: support-answer@2", "improved", "support-answer@2", 3.0),
            ("terse: support-answer@3", "terse", "support-answer@3", 1.0),
        ]
        if all(r[0] in existing for r in runs):
            self.say("experiments: already seeded, skipped")
            return
        examples = [
            Example(i["input"], i.get("expected"), i.get("metadata") or {})
            for i in ds_detail["items"]
        ]
        ds = Dataset(DATASET, examples)
        judge = ScriptedJudge(RUBRIC_V2)
        scorers = [build_scorer("contains_expected"), build_scorer("wer"), judge]

        class NullSink:
            def save_span(self, span: Any) -> None: ...
            def save_spans(self, spans: Any) -> None: ...

        from tirkit.tracing import sink_context

        results = {}
        for name, system, prompt, days_ago in runs:
            if name in existing:
                continue

            def target(inp: dict[str, Any], _s: str = system) -> str:
                return system_output(_s, str(inp.get("question", "")))

            with sink_context(NullSink()):
                res = run(
                    ds,
                    target,
                    scorers,
                    name=name,
                    concurrency=1,
                    config={
                        "prompt_version": prompt,
                        "model": "gpt-4o-mini",
                        "params": {"temperature": 0},
                        "synthetic": True,
                        "note": SYNTHETIC,
                    },
                )
            res.dataset_version = ds_detail["version"]
            res.target = f"{prompt} · gpt-4o-mini (simulated)"
            res.created = self.now - days_ago * DAY
            n = len(examples)
            res.trace_ids = [None] * n
            # Latency and cost are invented (the targets are scripts), so say so in the config.
            res.latencies_ms = [
                700 + 1600 * unit("lat", system, i) ** 2 + (400 if system == "improved" else 0)
                for i in range(n)
            ]
            res.costs_usd = [
                round(
                    (0.00022 + 0.0006 * unit("cost", system, i))
                    * (1.35 if system == "improved" else 1),
                    6,
                )
                for i in range(n)
            ]
            res.config["latency_and_cost"] = "synthetic"
            self.req("POST", self.api("/experiments"), json=res.to_json())
            results[system] = res
        # Gate outcomes are not stored by the server; report them so the story is checkable.
        if {"baseline", "improved", "terse"} <= set(results):
            for cand in ("improved", "terse"):
                for scorer in ("contains_expected", "judge:correctness"):
                    ok = gate(results["baseline"], results[cand], scorer, n_resamples=2000)
                    self.say(f"  gate {cand} vs baseline on {scorer}: {'pass' if ok else 'FAIL'}")
        self.say(f"experiments: {len(results)} runs on {DATASET}@{ds_detail['version']}")

    # -- 5. annotation, labels and judge reliability ----------------------------------------------

    def annotation(self) -> str | None:
        cfgs = {c["name"] for c in self.req("GET", self.api("/score-configs")).json()}
        if "correctness" not in cfgs:
            self.req(
                "PUT",
                self.api("/score-configs/correctness"),
                json={
                    "data_type": "numeric",
                    "min_value": 1,
                    "max_value": 5,
                    "description": "How correct is the answer? 1 = wrong, 5 = fully correct",
                },
            )
        if "resolved" not in cfgs:
            self.req(
                "PUT",
                self.api("/score-configs/resolved"),
                json={
                    "data_type": "boolean",
                    "description": "Would this answer resolve the ticket?",
                },
            )
        queues = self.req("GET", self.api("/queues")).json()
        found = [q for q in queues if q["name"] == QUEUE_NAME]
        if found:
            self.say(
                f"annotation: queue already seeded ({found[0]['n_done']}/{found[0]['n_items']} labelled), skipped"
            )
            return str(found[0]["queue_id"])
        qid = self.req(
            "POST",
            self.api("/queues"),
            json={
                "name": QUEUE_NAME,
                "score_names": ["correctness", "resolved"],
                "description": "Rate each production answer. Synthetic dev data.",
            },
        ).json()["queue_id"]
        pool = [t for t in self.traffic if not t.error][-(N_LABELLED + N_UNLABELLED) :]
        self.req(
            "POST", self.api(f"/queues/{qid}/items"), json={"trace_ids": [t.trace_id for t in pool]}
        )
        by_trace = {t.trace_id: t for t in pool}
        for _ in range(N_LABELLED):
            nxt = self.req("GET", self.api(f"/queues/{qid}/next")).json()
            t = by_trace[nxt["item"]["trace_id"]]
            label = human_label(t.question, t.output)
            self.req(
                "POST",
                self.api(f"/queues/{qid}/items/{nxt['item']['item_id']}/complete"),
                json={"scores": {"correctness": label, "resolved": label >= 4}, "comment": None},
            )
        self.say(f"annotation: queue with {N_LABELLED} labelled and {N_UNLABELLED} left to label")
        return qid

    def judge_reports(self, qid: str | None) -> None:
        if self.store is None or qid is None:
            self.say("judge reports: skipped (needs the database: pass --db)")
            return
        from tirkit.annotation import Annotation
        from tirkit.reliability import judge_labels, list_reports, reliability_report, save_report

        source = f"queue:{qid}"
        have = {(r["rubric_version"], r["source"]) for r in list_reports(self.store)}
        rows = Annotation(self.store).labels(qid)
        made = []
        for rubric in (RUBRIC_V1, RUBRIC_V2):
            if (rubric.version, source) in have:
                continue
            judge = ScriptedJudge(rubric)
            scored = judge_labels(rows, judge, human_key="correctness", pairwise=True, seed=0)
            report = reliability_report(scored, human_key="correctness")
            report["synthetic"] = True
            save_report(self.store, report, rubric, JUDGE_MODEL, source)
            made.append(report)
            self.say(
                f"  report rubric {rubric.version}: kappa={report.get('kappa', float('nan')):.2f}"
                f" weighted={report.get('weighted_kappa', float('nan')):.2f}"
                f" flip_rate={report.get('flip_rate')} pairs={report['n_pairs_decided']}"
            )
            time.sleep(0.01)  # distinct created times keep the report order stable
        self.say(
            f"judge reports: {len(made)} created"
            if made
            else "judge reports: already seeded, skipped"
        )

    # -- 6. online evaluators and alerts ----------------------------------------------------------

    def online(self) -> None:
        if self.store is None:
            self.say("online evals: skipped (needs the database: pass --db)")
            return
        from tirkit.online import Evaluators

        class ScriptedEvaluators(Evaluators):
            """Same processing as the server's worker, with the scripted judge as the model."""

            def _scorer(self, cfg: dict[str, Any]) -> Any:
                name = str(cfg["spec"]).split(":", 1)[1]
                return ScriptedJudge(RUBRIC_V2, name=f"judge:{name}")

        have = {e["name"]: e for e in self.req("GET", self.api("/evaluators")).json()["evaluators"]}
        wanted = [
            (
                "online-correctness",
                {"spec": "judge:correctness", "model": JUDGE_MODEL},
                0.5,
                {"name": "support-agent"},
            ),
            (
                "online-faithfulness",
                {"spec": "judge:faithfulness", "model": JUDGE_MODEL},
                0.3,
                {"name": "support-agent", "release": "demo-v2"},
            ),
        ]
        ev = ScriptedEvaluators(self.store)
        for name, scorer, rate, flt in wanted:
            if name in have:
                continue
            eid = self.req(
                "POST",
                self.api("/evaluators"),
                json={
                    "name": name,
                    "scorer": scorer,
                    "sampling_rate": rate,
                    "filter": flt,
                    "backfill_s": 7 * DAY,
                },
            ).json()["evaluator_id"]
            # Pause while scoring so the server's own worker cannot process the same traces.
            self.req("PATCH", self.api(f"/evaluators/{eid}"), json={"active": False})
            e = next(x for x in ev.items() if x["evaluator_id"] == eid)
            for _ in range(20):
                e = next(x for x in ev.items() if x["evaluator_id"] == eid)
                st = ev.process(e, None)
                if not any(st.values()):
                    break
            self.req("PATCH", self.api(f"/evaluators/{eid}"), json={"active": True})
        done = ev.items()
        self.say("online evals: " + ", ".join(f"{e['name']} scored {e['n_scored']}" for e in done))

    def alerts(self) -> None:
        have = {a["name"] for a in self.req("GET", self.api("/alerts")).json()["alerts"]}
        # Email channel: with no SMTP settings the delivery fails and is recorded on the event,
        # which keeps the seed free of network calls.
        chan = {"type": "email", "to": "oncall@example.test"}
        rules = [
            (
                "Error rate above 3% (7 days)",
                {"metric": "error_rate", "op": ">", "threshold": 0.03, "window_s": 7 * DAY},
            ),
            (
                "p95 latency above 5 s (24 h)",
                {"metric": "p95_latency_ms", "op": ">", "threshold": 5000, "window_s": DAY},
            ),
            (
                "Online correctness below 3.0",
                {
                    "metric": "score_mean",
                    "op": "<",
                    "threshold": 3.0,
                    "window_s": 7 * DAY,
                    "score_name": "online-correctness",
                },
            ),
            (
                "Spend above 5 dollars (24 h)",
                {"metric": "cost_usd", "op": ">", "threshold": 5, "window_s": DAY},
            ),
        ]
        states = []
        for name, rule in rules:
            if name in have:
                continue
            aid = self.req(
                "POST", self.api("/alerts"), json={"name": name, "rule": rule, "channel": chan}
            ).json()["alert_id"]
            states.append(
                (name, self.req("POST", self.api(f"/alerts/{aid}/check")).json()["state"])
            )
        self.say(
            "alerts: "
            + (", ".join(f"{n} -> {s}" for n, s in states) if states else "already seeded, skipped")
        )

    # -- the whole thing --------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.log(f"Seeding development data. {SYNTHETIC.capitalize()}; for local use only.")
        self.login()
        self.new_key()
        self.traces()
        self.prompts()
        ds = self.dataset()
        self.experiments(ds)
        qid = self.annotation()
        self.judge_reports(qid)
        self.online()
        self.alerts()
        return self.counts()

    def counts(self) -> dict[str, Any]:
        g = lambda path: self.req("GET", self.api(path)).json()  # noqa: E731
        queues = g("/queues")
        reports = g("/judge-reports")["reports"] if self.store is not None else []
        return {
            "traces": g("/traces?limit=1").get("total"),
            "sessions": len(g("/sessions")),
            "users": len(g("/users")),
            "datasets": len(g("/datasets")),
            "dataset_versions": max(
                (len(g(f"/datasets/{d['name']}")["versions"]) for d in g("/datasets")), default=0
            ),
            "experiments": len(g("/experiments")),
            "queues": len(queues),
            "labelled": sum(q["n_done"] for q in queues),
            "unlabelled": sum(q["n_items"] - q["n_done"] for q in queues),
            "judge_reports": len(reports),
            "prompts": len(g("/prompts")),
            "evaluators": len(g("/evaluators")["evaluators"]),
            "online_scored": sum(e["n_scored"] for e in g("/evaluators")["evaluators"]),
            "alerts": len(g("/alerts")["alerts"]),
            "alert_events": len(g("/alerts")["events"]),
            "alerts_firing": sum(a["state"] == "firing" for a in g("/alerts")["alerts"]),
        }


def default_db() -> str | None:
    url = os.environ.get("TIRKIT_DATABASE_URL")
    if url:
        return url
    return "tirkit.db" if Path("tirkit.db").exists() else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument(
        "--db",
        default=None,
        help="the server's database (SQLite path or postgresql:// URL); default "
        "$TIRKIT_DATABASE_URL, else ./tirkit.db when it exists",
    )
    args = ap.parse_args()

    client = httpx.Client(base_url=args.url, timeout=60)
    seeder = Seeder(client)
    seeder.login()
    db = args.db or default_db()
    if db:
        from tirkit.store import open_store

        seeder.store = open_store(db).for_project(seeder.pid)
    else:
        print("No database given: judge reports and online-eval scores will be skipped.")
    counts = seeder.run()
    print(f"\nSeeded project {seeder.pid} at {args.url}. All of it is {SYNTHETIC}.")
    print("Login: see DEV_EMAIL and DEV_PASSWORD at the top of examples/seed_dev.py")
    print("Counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    sys.exit(main())
