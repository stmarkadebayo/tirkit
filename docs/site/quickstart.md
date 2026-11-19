# Quickstart

## 1. Run the server

```bash
pip install "tirkit[server]"     # the SDK, the server and the web UI (Python 3.11+)
tirkit serve                     # http://127.0.0.1:8000, data in ./tirkit.db
```

Open <http://127.0.0.1:8000>, create the admin account, and follow the **Send your first trace**
checklist on the Overview: it creates the key and shows the code with your address and key filled in
(or use **Settings → API keys → Create API key** and copy the three lines it shows).

This is one process on SQLite: enough for evaluation and a small team. For Postgres and a separate
background worker, use Docker:

```bash
git clone https://github.com/stmarkadebayo/tirkit && cd tirkit
docker compose up -d          # server, worker and Postgres on http://localhost:8000
```

**Already tracing without a server?** `tirkit.init()` with no settings writes `tirkit.db` in the
current folder. Run `tirkit serve` in the same folder and the first account you create opens on
those traces.

## 2. Send your first trace

In the app you are tracing, the SDK alone is enough:

```bash
pip install tirkit
export TIRKIT_HOST=http://localhost:8000 TIRKIT_PUBLIC_KEY=pk-tirkit-... TIRKIT_SECRET_KEY=sk-tirkit-...
```

```python
import tirkit
from openai import OpenAI

tirkit.init()  # reads TIRKIT_HOST / TIRKIT_PUBLIC_KEY / TIRKIT_SECRET_KEY
tirkit.instrument()  # OpenAI, Anthropic (and transformers) calls are traced automatically

client = OpenAI()


@tirkit.trace(kind="agent")
def answer(question: str) -> str:
    reply = client.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
    )
    return reply.choices[0].message.content


with tirkit.trace_context(user_id="user-42", session_id="chat-1"):
    answer("What's the capital of Kenya?")
tirkit.flush()
```

The checklist turns to "Got it" when the trace arrives. In **Traces**, the trace shows the agent span, the model call, tokens, cost and latency.

## 3. Evaluate a change

```bash
tirkit run evals/questions.jsonl app:answer -s exact_match_norm --out baseline.json
# change the prompt or model, then
tirkit run evals/questions.jsonl app:answer -s exact_match_norm --out candidate.json
tirkit compare baseline.json candidate.json      # means, paired difference and 95% CIs
tirkit gate baseline.json candidate.json -s exact_match_norm   # exit 1 only if confidently worse
```

With `TIRKIT_HOST` set, runs also appear under **Experiments**, where **Compare** shows the same
statistics with per-item diffs.

## No server at all

`tirkit.init()` without `TIRKIT_HOST` writes to a local `tirkit.db`; `tirkit traces list` and
`tirkit traces show <id>` read it, and every evaluation command works locally.
