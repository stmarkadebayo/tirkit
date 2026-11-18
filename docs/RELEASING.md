# Releasing

A release publishes three things from one tag: the Python package (PyPI), the TypeScript SDK (npm)
and the server image (GitHub Container Registry). `.github/workflows/release.yml` does the work;
the accounts and secrets below are set up once, by a maintainer.

## One-time setup

**PyPI (reserves the name `tirkit` on the first publish)**
1. Create an account on pypi.org and turn on two-factor sign-in.
2. Go to *Your projects → Publishing → Add a new pending publisher* and enter: project name
   `tirkit`, owner `stmarkadebayo`, repository `tirkit`, workflow `release.yml`, environment `pypi`.
3. In the GitHub repository, *Settings → Environments → New environment* named `pypi`.

No PyPI token is stored anywhere: the workflow uses trusted publishing.

**npm (for `@tirkit/sdk`)**
1. Create an account on npmjs.com, then an organisation named `tirkit` (free for public packages).
2. Create an automation access token that can publish to that organisation.
3. In the GitHub repository, create an environment named `npm` and add the token as a secret named
   `NPM_TOKEN`.

**GitHub Container Registry**
Nothing to set up: the workflow signs in with the repository's own token. After the first image is
published, open the package's settings on GitHub and make it public.

## Cutting a release

1. Update the version in `pyproject.toml` and `sdk-ts/package.json`, and move the `Unreleased`
   section of `CHANGELOG.md` under the new version.
2. Merge that to `main` once CI is green.
3. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`. The workflow refuses a tag that does
   not match the version in `pyproject.toml`.
4. Check the three registries, then install from them in a clean environment
   (`pip install "tirkit[server]"`, then `tirkit serve`) before announcing anything.

A manual run of the workflow (*Actions → release → Run workflow*) builds and checks the package but
publishes nothing, so it is safe to try before the first tag.

## What the build checks

The wheel must contain the web UI (`scripts/check_wheel.py`), otherwise `tirkit serve` from a pip
install would have no UI. `make dist` does the same locally.
