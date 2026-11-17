"""Every module must import with only the standard library installed (keeps CI light)."""

import importlib

import pytest

MODULES = [
    "tirkit",
    "tirkit.datasets",
    "tirkit.runner",
    "tirkit.scorers",
    "tirkit.stats",
    "tirkit.store",
    "tirkit.tracing",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports(name: str) -> None:
    importlib.import_module(name)


def test_cli_imports() -> None:
    pytest.importorskip("typer")
    importlib.import_module("tirkit.cli")
