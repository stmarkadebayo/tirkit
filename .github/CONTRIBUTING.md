# Contributing to Tirkit

Thanks for helping build Tirkit. This guide covers how to set up, what a good change looks like, and
the one-time Contributor License Agreement.

## Before you start
- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Read [SPEC.md](../docs/SPEC.md) (what Tirkit does and why) and [PLAN.md](../docs/PLAN.md) (what is being built now).
- Decisions that fill a gap in the spec, or depart from it, are recorded in
  [docs/DECISIONS.md](docs/DECISIONS.md). Add a row if your change makes one.

## Set up
```bash
make setup        # create .venv and install with dev tools
make test         # all tests
make test-core    # tests that do not need the server
make test-server  # server, API contract and Postgres tests
make test-web     # web UI: generated API types current + typecheck (needs npm ci in web/)
make lint         # ruff, mypy and the layer contracts (import-linter)
make api          # regenerate docs/api/openapi.json and the web UI's API types
make progress     # how many acceptance tests pass
```
Python 3.11 or newer. Copy `.env.example` to `.env` for anything that calls an LLM; never commit `.env`.

## Where do I change X?

Tirkit is split into layers, and `make lint` fails if code imports across them the wrong way. Each
layer may import only from layers below it in this table; the web UI talks to the server only
through the HTTP API.

| I want to change... | Change it in | Layer | Run |
| --- | --- | --- | --- |
| A data type shared everywhere (`Span`, `Example`, `Dataset`, `Score`, `RunResult`) | `src/tirkit/types.py` | core | `make test` |
| Redaction, prices and units, JSON conversion, statistics | `redact.py`, `pricing.py`, `serialize.py`, `stats.py` | core | `make test-core` |
| How spans, datasets, experiments or scores are stored or queried | `src/tirkit/store/` (one module per domain) | storage | `make test-core`, then `make test-server` |
| Tracing API, the background exporter, `tirkit.init()` | `tracing.py`, `exporter.py`, `client.py` | sdk | `make test-core`, `make bench` |
| Support for a provider SDK (OpenAI, Anthropic, Gemini, ...) | `src/tirkit/instrumentation/` ([README](../src/tirkit/instrumentation/README.md)) | sdk | `make test-core`, `make bench` |
| A framework integration (LangChain, transformers) | `src/tirkit/integrations/` | sdk | `make test-core` |
| OTLP decoding and attribute mapping | `otel.py` | sdk | `make test-server` |
| Scorers, the LLM judge, rubrics, judge reliability | `scorers.py`, `scorer_specs.py`, `judge.py`, `reliability.py` | eval | `make test-core` |
| Running, comparing and gating experiments; reports | `runner.py`, `report.py` | eval | `make test-core` |
| Accounts, alerts, annotation, online evaluators, prompts, background jobs | `accounts.py`, `alerts.py`, `annotation.py`, `online.py`, `prompts.py`, `jobs.py`, `worker_tasks.py` | services | `make test` |
| An HTTP route or its request/response shape | `src/tirkit/server/routers/` and `models.py` ([README](../src/tirkit/server/README.md)); then `make api` | delivery | `make test-server`, `make api-check` |
| A CLI command | `cli.py` | delivery | `make test-core` |
| A screen in the web UI | `web/src/pages/` ([README](../web/README.md)) | web | `make test-web` |
| The TypeScript SDK | `sdk-ts/` ([README](../sdk-ts/README.md)) | sdk (TS) | `cd sdk-ts && npm test` |
| User docs | `docs/site/` | docs | `mkdocs build --strict` |
| Docker, Compose, CI | `Dockerfile`, `docker-compose.yml`, `.github/workflows/` | deploy | — |

Keep a pull request to one area where you can. Pull requests are labelled by area automatically, and
CI runs only the jobs for the areas you touched. If a change needs a type from a higher layer, move
the type down (usually into `tirkit/types.py`) rather than adding an exception to `.importlinter`.

## Making a change
1. Fork the repository and create a branch from `main`.
2. Write or extend tests first. Acceptance tests in `tests/test_acceptance.py` come from SPEC.md;
   never weaken, skip or delete a test to make it pass. If a test is wrong, explain why in your pull
   request and fix it in its own commit.
3. Keep the change small and focused. Add type hints and docstrings on public functions.
4. Keep heavy imports (fastapi, torch and similar) inside functions so the core SDK stays light.
5. Run `make test` and `make lint` before you push (and `make api` if you changed the HTTP API).
6. Write short, plain commit messages: one line saying what changed, such as `Add login rate limit`
   or `Fix span ordering in trace view`. No prefixes. One logical change per commit.
7. Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for any user-visible change.
8. Open a pull request and fill in the template.

## Results and numbers
Tirkit's README reports only measured numbers. If your change affects a reported metric, include how
you measured it. Never estimate or round up a result.

## Contributor License Agreement
Before we can merge your first pull request, you need to sign the
[Contributor License Agreement](CLA.md) once. A bot will comment on your pull request; reply with:

> I have read the CLA Document and I hereby sign the CLA

That signature covers all your future contributions. In short, the CLA says you keep the copyright
in your work and grant the project a broad licence to use it, including the right to offer Tirkit under
other licence terms in future. Read [CLA.md](CLA.md) for the full text. If you contribute as part of
your job, check that your employer allows it first.

## Security issues
Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Code of Conduct
Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Licence
Tirkit is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your
contributions are also covered by the [CLA](CLA.md).
