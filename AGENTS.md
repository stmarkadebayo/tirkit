# AGENTS.md: Tirkit

Instructions for coding agents and humans working in this repository.

Tirkit is an open-source LLM observability and evaluation platform: tracing, test sets mined from
production traces, CI regression gates, and an LLM judge whose reliability is measured.

## Git and attribution (read first)

**Do not credit any AI, and do not add an AI as a contributor, in anything pushed to GitHub.**

- No `Co-Authored-By` trailers, no "Generated with ..." lines, and no AI names in commit
  messages, pull requests, release notes, changelogs or code comments.
- Commit as the configured git identity. Never change `user.name` or `user.email`.
- Commit messages are short and plain: one line saying what changed, for example
  `Add login rate limit` or `Fix span ordering in trace view`. No prefixes, no long bodies.
- Keep commits small: one logical change each.

## Where things are

Read these before changing code: [README.md](README.md), [docs/SPEC.md](docs/SPEC.md) (what to
build and the decisions already made) and [docs/PLAN.md](docs/PLAN.md) (milestones and their
definitions of done). [docs/DECISIONS.md](docs/DECISIONS.md) records every choice that fills a gap
in the spec. [docs/ROADMAP.md](docs/ROADMAP.md) lists planned work.

## How to work

1. Work in the order of docs/PLAN.md. Start a milestone only when the previous one's "Done when" is true.
2. Write or extend tests first (acceptance tests come from docs/SPEC.md), then implement, then run
   `make test` and `make lint`. `make progress` runs the acceptance tests only.
3. If the spec is silent, ambiguous or wrong, pick the simplest option that works, record it in
   docs/DECISIONS.md, and say so in your summary.
4. Tracing runs inside other people's apps. Anything on the hot path must not add noticeable latency;
   re-run `research/benchmarks/bench_tracing.py` after touching it.
5. Update the docs in `docs/site/` and the CHANGELOG in the same change as the code.
6. At the end of a session, summarise what changed, which tests pass, what was measured, and what is open.

## Keep areas separate

Tirkit is open source, so a change in one area must not break another. Core logic (types, scoring,
statistics, storage) does not import the server, CLI or web app. The web app talks to the server only
through the HTTP API and its generated types, never through Python code. Put a change in the layer it
belongs to, and keep pull requests to one area. See the "where do I change X" table in
`.github/CONTRIBUTING.md`. The layers are enforced by `import-linter` (`.importlinter`, part of
`make lint`). After changing an HTTP route or `server/models.py`, run `make api` and commit
`docs/api/openapi.json` and `web/src/api/schema.ts`.

## Hard rules

- **Never invent, estimate or round up results.** README Results holds only measured numbers, each
  with how it was measured. If something has not been measured, leave the cell as it is.
- **Never weaken, skip or delete a test to make it pass.** If a test is wrong, explain why in
  docs/DECISIONS.md and fix it in its own commit.
- **No secrets in the repo.** Keys live in `.env` (git-ignored) and CI secrets.
- **Keep CI light:** heavy imports (torch, fastapi, redis and similar) go inside functions.
- **Cost:** no paid services beyond the budgets in `.env` without a maintainer's approval.
- **Data:** use only the datasets named in docs/SPEC.md, respect their licences, and never commit raw downloads.
- **Style:** Python 3.11+, type hints, ruff, small modules, docstrings on public functions.

## Commands

```bash
make setup      # create .venv and install
make test       # all tests
make progress   # acceptance tests only
make lint       # ruff, mypy and layer contracts (import-linter)
make test-core  # tests that do not need the server
make test-server # server, API contract and Postgres tests
make test-web   # web UI: generated API types current + typecheck
make api        # regenerate docs/api/openapi.json and web/src/api/schema.ts
make demo       # run examples/quickstart.py
make web        # build the web UI
make serve      # run the server
make bench      # tracing overhead benchmark
```

## Layout

```
src/tirkit/         Python package: types, tracing, client, store/, datasets, scorers, judge, stats, runner, cli
  instrumentation/  OpenAI, Anthropic, Gemini and Realtime auto-instrumentation
  integrations/     LangChain, transformers
  store/            SQLite and Postgres storage, one module per domain
  server/           FastAPI app: deps (auth), models (API contract), routers/ per domain
web/              React + TypeScript UI
sdk-ts/           TypeScript SDK
tests/            unit, server and acceptance tests
examples/         quickstart, demo apps, CI workflow example
research/         benchmarks, experiments (E1, E2) and the QA set they use
docs/             SPEC, PLAN, DECISIONS, ROADMAP, the API schema (docs/api) and the docs site (docs/site)
```
