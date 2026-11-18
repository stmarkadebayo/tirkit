"""Release invariants: what the registries and the release workflow need to publish."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/stmarkadebayo/tirkit"


def test_the_npm_package_can_be_published_with_provenance() -> None:
    pkg = json.loads((ROOT / "sdk-ts" / "package.json").read_text())
    # `npm publish --provenance` fails unless repository.url matches the GitHub repository.
    assert pkg["repository"]["url"] == f"git+{REPO}.git"
    assert pkg["repository"]["directory"] == "sdk-ts"
    assert (ROOT / "sdk-ts" / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()
    assert pkg["name"] == "@tirkit/sdk"


def test_the_python_and_npm_versions_agree_and_pypi_shows_project_links() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pkg = json.loads((ROOT / "sdk-ts" / "package.json").read_text())
    assert project["version"] == pkg["version"]  # one tag releases both
    assert project["urls"]["Repository"] == REPO
    assert project["dependencies"]  # a stray table header would swallow these
