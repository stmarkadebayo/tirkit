"""Prepare the 100 outputs of experiment E1 for human labelling.

Sends the answers cached by `research/experiments/e1_judge_bias.py` (a one-sentence and a 3-5
sentence answer to each of the 50 questions in `research/evals/acme_qa.jsonl`) to a running Tirkit
server as traces, defines the `correct` score (1-5), and fills an annotation queue with them in a
fixed random order. The reference answer is part of each trace's input, so the human and the judge
see the same information (the judge report sees only a trace's input and output).

    python research/labelling/prepare_e1_queue.py --url http://localhost:8780 --home ~/tirkit-labelling

On a fresh server it creates the admin account with a random password and saves it to
``<home>/LOGIN.txt`` (mode 600). Safe to run again: it does nothing if the queue already exists.
It writes ``<home>/items.json``, which maps each trace to the prompt variant that produced it, so
the variant is not visible while labelling but can be used in the analysis.

No model is called. Nothing here is measured data: the labels you add afterwards are.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
QA = ROOT / "research" / "evals" / "acme_qa.jsonl"
ANSWERS = ROOT / "research" / "experiments" / "results" / "e1_answers.json"
GUIDE = Path(__file__).with_name("GUIDE.md")
SCORE = "correct"
QUEUE_NAME = "E1 judge labels"
SCORE_HELP = (
    "5 every fact in the reference is stated correctly · 4 correct, misses one part · "
    "3 about half right · 2 mostly wrong or contradicts the reference · 1 wrong or refused. "
    "Length earns nothing; extra detail that does not contradict the reference is ignored."
)


def load_items() -> list[dict]:
    answers = json.loads(ANSWERS.read_text())
    items = []
    for line in QA.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        question = row["input"]["question"]
        for variant in ("brief", "detailed"):
            items.append(
                {
                    "variant": variant,
                    "question": question,
                    "reference": row["expected"],
                    "answer": answers[question][variant],
                }
            )
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8780")
    ap.add_argument(
        "--home", default="~/tirkit-labelling", help="where LOGIN.txt and items.json go"
    )
    ap.add_argument("--seed", type=int, default=1, help="queue order")
    args = ap.parse_args()
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    url = args.url.rstrip("/")
    items = load_items()
    if len(items) != 100:
        raise SystemExit(f"expected 100 items, found {len(items)}")

    with httpx.Client(base_url=url, timeout=30) as c:
        if c.get("/api/setup").json()["needs_setup"]:
            email = "labeller@tirkit.local"
            password = secrets.token_urlsafe(18)
            r = c.post(
                "/api/setup",
                json={
                    "email": email,
                    "password": password,
                    "name": "Labeller",
                    "project_name": "Judge labels",
                },
            )
            r.raise_for_status()
            login = home / "LOGIN.txt"
            login.write_text(f"{url}\nemail: {email}\npassword: {password}\n")
            login.chmod(0o600)
            print(f"created the admin account; login saved to {login}")
        else:
            login = home / "LOGIN.txt"
            if not login.exists():
                raise SystemExit(f"the server is already set up and {login} is missing")
            _, e, p = login.read_text().splitlines()[:3]
            email, password = e.split(": ", 1)[1], p.split(": ", 1)[1]
        c.post("/api/auth/login", json={"email": email, "password": password}).raise_for_status()
        c.headers["X-CSRF-Token"] = c.cookies.get("tirkit_csrf") or ""
        pid = c.get("/api/auth/me").json()["projects"][0]["project_id"]

        if any(q.get("name") == QUEUE_NAME for q in c.get(f"/api/projects/{pid}/queues").json()):
            print(f"the queue {QUEUE_NAME!r} already exists; nothing to do")
            return 0

        key = c.post(f"/api/projects/{pid}/keys", json={"name": "e1-labelling"}).json()
        import tirkit

        tirkit.init(host=url, public_key=key["public_key"], secret_key=key["secret_key"])
        for it in items:
            payload = {"question": it["question"], "reference_answer": it["reference"]}
            with tirkit.span("support-answer", kind="llm", input=payload) as s:
                s.output = it["answer"]
        tirkit.flush()

        deadline = time.monotonic() + 60
        traces: list[dict] = []
        while time.monotonic() < deadline:
            traces = c.get(f"/api/projects/{pid}/traces", params={"limit": 500}).json()["items"]
            if len(traces) >= len(items):
                break
            time.sleep(1)
        if len(traces) < len(items):
            raise SystemExit(f"only {len(traces)} of {len(items)} traces arrived")

        # Some answers are word for word the same for different questions, so match on both.
        def key(question: str, answer: str) -> tuple[str, str]:
            return question, answer

        by_item = {key(t["input"]["question"], t["output"]): t["trace_id"] for t in traces}
        mapping = {}
        for i, it in enumerate(items):
            mapping[by_item[key(it["question"], it["answer"])]] = {"item": i, **it}
        if len(mapping) != len(items):
            raise SystemExit(f"matched {len(mapping)} traces to {len(items)} items")
        (home / "items.json").write_text(json.dumps(mapping, indent=1))

        c.put(
            f"/api/projects/{pid}/score-configs/{SCORE}",
            json={
                "data_type": "numeric",
                "min_value": 1,
                "max_value": 5,
                "description": SCORE_HELP,
            },
        ).raise_for_status()
        q = c.post(
            f"/api/projects/{pid}/queues",
            json={
                "name": QUEUE_NAME,
                "score_names": [SCORE],
                "description": GUIDE.read_text(),
            },
        )
        q.raise_for_status()
        qid = q.json()["queue_id"]
        order = list(mapping)
        random.Random(args.seed).shuffle(order)
        added = c.post(f"/api/projects/{pid}/queues/{qid}/items", json={"trace_ids": order})
        added.raise_for_status()
        print(f"queue {QUEUE_NAME!r}: {added.json()}")
    print(f"done. Open {url} and label the queue (guide: {GUIDE.relative_to(ROOT)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
