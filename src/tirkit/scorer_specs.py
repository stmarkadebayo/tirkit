"""Build scorers from short text specs such as ``exact_match`` or ``judge:correctness``.

Shared by the CLI (``tirkit run -s ...``), the worker (UI-started experiments) and online
evaluators, so the spec language is defined once, in the evaluation layer.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any


class UnknownScorer(ValueError):
    """The spec names no known scorer."""


def load_object(spec: str) -> Any:
    """Import ``module.path:attr.path`` relative to the current directory."""
    module, _, attr = spec.partition(":")
    sys.path.insert(0, os.getcwd())
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def build_scorer(spec: str, store: Any = None) -> Any:
    """Scorer from a short name: exact_match, exact_match_norm, wer, cer, json_valid,
    contains_expected, judge:<rubric> (correctness, faithfulness, ...), or module:Class."""
    from tirkit import scorers as sc

    if spec == "exact_match":
        return sc.ExactMatch()
    if spec == "exact_match_norm":
        return sc.ExactMatch(normalise=True)
    if spec == "wer":
        return sc.WordErrorRate()
    if spec == "cer":
        return sc.CharacterErrorRate()
    if spec == "json_valid":
        return sc.RuleScorer([sc.rules.json_valid()], name="json_valid")
    if spec == "contains_expected":

        def contains(example: Any, output: Any) -> float:
            return float(sc.output_text(example.expected).lower() in sc.output_text(output).lower())

        return sc.FunctionScorer(contains, name="contains_expected")
    if spec.startswith("judge:"):
        from tirkit.judge import LLMJudge, OpenAICompatibleJudge

        return LLMJudge(OpenAICompatibleJudge(), spec.split(":", 1)[1], cache=store)
    if ":" in spec:
        obj = load_object(spec)
        return obj() if isinstance(obj, type) else obj
    raise UnknownScorer(f"unknown scorer {spec!r}")


def build_scorers(specs: list[str], store: Any, judge_model: str | None) -> list[Any]:
    """Scorers for server-side runs: judges use the server's configured model, and
    ``module:Class`` specs are refused because they would import arbitrary code."""
    from tirkit.judge import LLMJudge
    from tirkit.providers import ServerJudgeModel

    out = []
    for spec in specs:
        if spec.startswith("judge:"):
            if not judge_model:
                raise ValueError("judge scorers need a judge model")
            out.append(LLMJudge(ServerJudgeModel(judge_model), spec.split(":", 1)[1], cache=store))
        elif ":" in spec:
            raise ValueError(f"scorer {spec!r} is not allowed from the UI")
        else:
            out.append(build_scorer(spec, store))
    return out
