"""Scorers turn (example, output) into a score with a reason (SPEC FR-13, FR-33, §6.5).

Every scorer has ``name``, ``version`` and ``higher_is_better``. A scorer that cannot produce a
score returns ``Score(value=None, error=...)``: failures are recorded, never silently scored.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

from tirkit.media import Attachment, MediaStore, is_media_ref
from tirkit.serialize import to_jsonable
from tirkit.types import Example
from tirkit.types import Score as Score


class Scorer(Protocol):
    name: str

    def score(self, example: Example, output: Any) -> Score: ...


def scorer_version(s: Any) -> str:
    return str(getattr(s, "version", "1"))


def higher_is_better(s: Any) -> bool:
    return bool(getattr(s, "higher_is_better", True))


# -- helpers ------------------------------------------------------------------------------------


def output_text(output: Any) -> str:
    """Best-effort text from common output shapes (str, chat message, content blocks, dicts)."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    output = to_jsonable(output)
    if isinstance(output, dict):
        for key in ("output_text", "content", "text", "answer", "output", "transcript"):
            if key in output and output[key] is not None:
                return output_text(output[key])
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    if isinstance(output, list):
        parts = [
            output_text(p.get("text") if isinstance(p, dict) and "text" in p else p)
            for p in output
            if not (isinstance(p, dict) and p.get("type") in {"tool_use", "thinking", "image"})
        ]
        return "\n".join(x for x in parts if x)
    return str(output)


_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


def normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def edit_distance(a: Sequence[Any], b: Sequence[Any]) -> int:
    """Levenshtein distance between two sequences."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word edits ÷ reference words, after normalising case and punctuation."""
    ref = normalise_text(reference).split()
    hyp = normalise_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate (spaces ignored)."""
    ref = normalise_text(reference).replace(" ", "")
    hyp = normalise_text(hypothesis).replace(" ", "")
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


# -- deterministic scorers ----------------------------------------------------------------------


class ExactMatch:
    """1.0 when the output equals the expected value (optionally after normalising text)."""

    name = "exact_match"
    version = "1"
    higher_is_better = True

    def __init__(self, normalise: bool = False) -> None:
        self.normalise = normalise
        if normalise:
            self.version = "1n"

    def score(self, example: Example, output: Any) -> Score:
        if self.normalise:
            return Score(
                float(
                    normalise_text(output_text(output))
                    == normalise_text(output_text(example.expected))
                )
            )
        return Score(float(output == example.expected))


Rule = Callable[[Any], tuple[bool, str]]


class rules:  # noqa: N801 - namespace of rule constructors
    """Named checks for :class:`RuleScorer`. Each returns ``(passed, reason)``."""

    @staticmethod
    def regex(pattern: str, *, must_match: bool = True, flags: int = 0) -> Rule:
        rx = re.compile(pattern, flags)

        def check(output: Any) -> tuple[bool, str]:
            found = rx.search(output_text(output)) is not None
            ok = found if must_match else not found
            verb = "matches" if found else "does not match"
            return ok, f"output {verb} /{pattern}/"

        check.__name__ = f"regex:{pattern}"
        return check

    @staticmethod
    def max_length(n: int, unit: Literal["chars", "words"] = "chars") -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            text = output_text(output)
            size = len(text.split()) if unit == "words" else len(text)
            return size <= n, f"{size} {unit} (max {n})"

        check.__name__ = f"max_length:{n}{unit}"
        return check

    @staticmethod
    def min_length(n: int, unit: Literal["chars", "words"] = "chars") -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            text = output_text(output)
            size = len(text.split()) if unit == "words" else len(text)
            return size >= n, f"{size} {unit} (min {n})"

        check.__name__ = f"min_length:{n}{unit}"
        return check

    @staticmethod
    def banned_phrases(phrases: Sequence[str], *, case_sensitive: bool = False) -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            text = output_text(output)
            hay = text if case_sensitive else text.lower()
            hits = [p for p in phrases if (p if case_sensitive else p.lower()) in hay]
            return not hits, f"banned phrases found: {hits}" if hits else "no banned phrases"

        check.__name__ = "banned_phrases"
        return check

    @staticmethod
    def contains(substring: str, *, case_sensitive: bool = False) -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            text = output_text(output)
            ok = substring in text if case_sensitive else substring.lower() in text.lower()
            return ok, f"{'contains' if ok else 'missing'} {substring!r}"

        check.__name__ = f"contains:{substring}"
        return check

    @staticmethod
    def json_valid() -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            if isinstance(output, dict | list):
                return True, "already structured"
            try:
                json.loads(output_text(output))
                return True, "valid JSON"
            except ValueError as exc:
                return False, f"invalid JSON: {exc}"

        check.__name__ = "json_valid"
        return check

    @staticmethod
    def json_schema(schema: dict[str, Any]) -> Rule:
        def check(output: Any) -> tuple[bool, str]:
            data = output
            if not isinstance(output, dict | list):
                try:
                    data = json.loads(output_text(output))
                except ValueError as exc:
                    return False, f"invalid JSON: {exc}"
            errors = validate_json_schema(data, schema)
            return not errors, "; ".join(errors[:5]) if errors else "matches schema"

        check.__name__ = "json_schema"
        return check


_JSON_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def validate_json_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the common JSON Schema keywords (type, required, properties,
    additionalProperties, items, enum, const, min/max length/items, minimum/maximum, anyOf)."""
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]

        def is_type(name: str) -> bool:
            if name == "integer":
                return isinstance(data, int) and not isinstance(data, bool)
            if name == "number":
                return isinstance(data, int | float) and not isinstance(data, bool)
            py = _JSON_TYPES.get(name)
            return (
                py is not None
                and isinstance(data, py)
                and not (py is not bool and isinstance(data, bool))
            )

        if not any(is_type(n) for n in types):
            return [f"{path}: expected {t}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: {data!r} not in {schema['enum']}")
    if "const" in schema and data != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}")
    if "anyOf" in schema and not any(
        not validate_json_schema(data, sub, path) for sub in schema["anyOf"]
    ):
        errs.append(f"{path}: matches none of anyOf")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errs.append(f"{path}: missing required {key!r}")
        props = schema.get("properties", {})
        for key, value in data.items():
            if key in props:
                errs.extend(validate_json_schema(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property {key!r}")
    if isinstance(data, list):
        if "items" in schema:
            for i, item in enumerate(data):
                errs.extend(validate_json_schema(item, schema["items"], f"{path}[{i}]"))
        if "minItems" in schema and len(data) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errs.append(f"{path}: does not match pattern")
    if isinstance(data, int | float) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")
    return errs


class RuleScorer:
    """Deterministic checks: score is the fraction of rules passed; the reason lists failures."""

    name = "rules"
    higher_is_better = True

    def __init__(self, rules: Sequence[Rule], name: str = "rules") -> None:
        self.rules = list(rules)
        self.name = name
        names = ",".join(getattr(r, "__name__", "rule") for r in self.rules)
        self.version = hashlib.sha256(names.encode()).hexdigest()[:8]

    def score(self, example: Example, output: Any) -> Score:
        if not self.rules:
            return Score(None, error="no rules")
        failures, details = [], {}
        for rule in self.rules:
            rname = getattr(rule, "__name__", "rule")
            try:
                ok, reason = rule(output)
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"rule raised {type(exc).__name__}: {exc}"
            details[rname] = {"passed": ok, "reason": reason}
            if not ok:
                failures.append(f"{rname}: {reason}")
        passed = len(self.rules) - len(failures)
        return Score(
            passed / len(self.rules),
            reason="all rules passed" if not failures else "; ".join(failures),
            metadata={"rules": details},
        )


class FunctionScorer:
    """Wrap ``fn(example, output)`` returning a float, bool, (value, reason) or :class:`Score`."""

    version = "1"

    def __init__(
        self,
        fn: Callable[[Example, Any], Any],
        name: str | None = None,
        higher_is_better: bool = True,
    ) -> None:
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "function")
        self.higher_is_better = higher_is_better
        try:
            self.version = hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()[:8]
        except (OSError, TypeError):
            pass

    def score(self, example: Example, output: Any) -> Score:
        try:
            result = self.fn(example, output)
        except Exception as exc:  # noqa: BLE001
            return Score(None, error=f"{type(exc).__name__}: {exc}")
        if isinstance(result, Score):
            return result
        if isinstance(result, tuple):
            return Score(float(result[0]), str(result[1]) if len(result) > 1 else None)
        return Score(None if result is None else float(result))


# -- speech -------------------------------------------------------------------------------------


class WordErrorRate:
    """ASR quality: WER of the output transcript against ``example.expected`` (lower is better)."""

    name = "wer"
    version = "1"
    higher_is_better = False

    def score(self, example: Example, output: Any) -> Score:
        if example.expected is None:
            return Score(None, error="no reference transcript in example.expected")
        ref, hyp = output_text(example.expected), output_text(output)
        return Score(wer(ref, hyp), reason=f"reference={ref[:80]!r} hypothesis={hyp[:80]!r}")


class CharacterErrorRate:
    """ASR quality for languages without spaces, or names and numbers (lower is better)."""

    name = "cer"
    version = "1"
    higher_is_better = False

    def score(self, example: Example, output: Any) -> Score:
        if example.expected is None:
            return Score(None, error="no reference transcript in example.expected")
        return Score(cer(output_text(example.expected), output_text(output)))


def _audio_bytes(obj: Any, media: MediaStore | None) -> tuple[bytes, str | None] | None:
    obj = to_jsonable(obj) if not isinstance(obj, bytes | Attachment) else obj
    if isinstance(obj, bytes):
        return obj, None
    if isinstance(obj, Attachment):
        return obj.read(), obj.mime
    if is_media_ref(obj):
        data = media.get(obj["sha256"]) if media is not None else None
        return (data, obj.get("mime")) if data is not None else None
    if isinstance(obj, dict):
        for key in ("audio", "data", "output"):
            if key in obj:
                found = _audio_bytes(obj[key], media)
                if found:
                    return found
    return None


class TTSRoundTrip:
    """TTS intelligibility: transcribe the output audio with a reference ASR model, then WER
    against the text that was spoken (lower is better).

    ``transcribe(audio_bytes, mime) -> str``. The text comes from ``example.input["text"]`` (or
    ``input``), else ``example.expected``.
    """

    name = "tts_round_trip_wer"
    version = "1"
    higher_is_better = False

    def __init__(
        self, transcribe: Callable[[bytes, str | None], str], media_store: MediaStore | None = None
    ) -> None:
        self.transcribe = transcribe
        self.media_store = media_store

    def score(self, example: Example, output: Any) -> Score:
        text = example.input.get("text") or example.input.get("input") or example.expected
        if not text:
            return Score(None, error="no source text in example")
        audio = _audio_bytes(output, self.media_store)
        if audio is None:
            return Score(None, error="no audio in output")
        try:
            transcript = self.transcribe(*audio)
        except Exception as exc:  # noqa: BLE001
            return Score(None, error=f"transcription failed: {exc}")
        return Score(
            wer(str(text), transcript),
            reason=f"heard {transcript[:80]!r}",
            metadata={"transcript": transcript},
        )


# -- similarity ---------------------------------------------------------------------------------


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingSimilarity:
    """Cosine similarity between output and expected text, with your embedding function."""

    name = "embedding_similarity"
    version = "1"
    higher_is_better = True

    def __init__(self, embed: Callable[[list[str]], list[list[float]]]) -> None:
        self.embed = embed

    def score(self, example: Example, output: Any) -> Score:
        if example.expected is None:
            return Score(None, error="no expected output")
        try:
            a, b = self.embed([output_text(output), output_text(example.expected)])
        except Exception as exc:  # noqa: BLE001
            return Score(None, error=f"embedding failed: {exc}")
        return Score(cosine(a, b))


# -- LLM judge ----------------------------------------------------------------------------------


def __getattr__(name: str) -> Any:
    # LLMJudge lives in tirkit.judge (which imports this module); re-export it lazily.
    if name in {"LLMJudge", "PairwiseResult", "Rubric"}:
        from tirkit import judge

        return getattr(judge, name)
    raise AttributeError(name)


__all__ = [
    "CharacterErrorRate",
    "EmbeddingSimilarity",
    "ExactMatch",
    "FunctionScorer",
    "RuleScorer",
    "Score",
    "Scorer",
    "TTSRoundTrip",
    "WordErrorRate",
    "cer",
    "output_text",
    "rules",
    "wer",
]
