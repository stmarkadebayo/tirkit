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
make setup      # create .venv and install with dev tools
make test       # all tests
make lint       # ruff + mypy
make progress   # how many acceptance tests pass
```
Python 3.11 or newer. Copy `.env.example` to `.env` for anything that calls an LLM; never commit `.env`.

## Making a change
1. Fork the repository and create a branch from `main`.
2. Write or extend tests first. Acceptance tests in `tests/test_acceptance.py` come from SPEC.md;
   never weaken, skip or delete a test to make it pass. If a test is wrong, explain why in your pull
   request and fix it in its own commit.
3. Keep the change small and focused. Add type hints and docstrings on public functions.
4. Keep heavy imports (fastapi, torch and similar) inside functions so the core SDK stays light.
5. Run `make test` and `make lint` before you push.
6. Use [Conventional Commits](https://www.conventionalcommits.org/) for messages: `feat:`, `fix:`,
   `test:`, `docs:`, `refactor:`, `chore:`.
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
