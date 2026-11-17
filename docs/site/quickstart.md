# Quickstart

## 1. Run the server

```bash
git clone https://github.com/stmarkadebayo/tirkit && cd tirkit
docker compose up -d          # server, worker and Postgres on http://localhost:8000
```

Open <http://localhost:8000>, create the admin account, then **Settings → API keys → Create API key**.
Copy the three lines it shows.

No Docker? `pip install "tirkit[server]" && tirkit serve` runs everything in one process on SQLite.

## 2. Send your first trace

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

Refresh **Traces**: the trace shows the agent span, the model call, tokens, cost and latency.

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
