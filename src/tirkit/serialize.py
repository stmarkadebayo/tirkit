"""Turn arbitrary Python objects (pydantic models, dataclasses, SDK types) into JSON-safe trees.

Bytes and :class:`tirkit.media.Attachment` are kept as-is so media extraction can store them later,
off the hot path.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import uuid
from pathlib import PurePath
from typing import Any

from tirkit.media import Attachment

MAX_DEPTH = 32
MAX_REPR = 2000


def _is_sentinel(obj: Any) -> bool:
    # openai.NOT_GIVEN / Omit, anthropic.NOT_GIVEN: "argument not passed"
    return type(obj).__name__ in {"NotGiven", "Omit", "_NotGiven"}


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """Best-effort conversion; never raises."""
    try:
        return _convert(obj, _depth)
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        return _short_repr(obj)


def _short_repr(obj: Any) -> str:
    try:
        text = repr(obj)
    except Exception:  # noqa: BLE001
        text = f"<{type(obj).__name__}>"
    return text if len(text) <= MAX_REPR else text[:MAX_REPR] + "…"


def _convert(obj: Any, depth: int) -> Any:
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if depth > MAX_DEPTH:
        return _short_repr(obj)
    if isinstance(obj, bytes | bytearray | memoryview | Attachment):
        return obj
    if isinstance(obj, dict):
        return {str(k): _convert(v, depth + 1) for k, v in obj.items() if not _is_sentinel(v)}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_convert(v, depth + 1) for v in obj]
    if isinstance(obj, enum.Enum):
        return _convert(obj.value, depth + 1)
    if isinstance(obj, dt.datetime | dt.date | dt.time):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID | PurePath):
        return str(obj)
    if _is_sentinel(obj):
        return None
    dump = getattr(obj, "model_dump", None)  # pydantic v2 (OpenAI and Anthropic SDK types)
    if callable(dump):
        return _convert(dump(), depth + 1)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _convert(getattr(obj, f.name), depth + 1) for f in dataclasses.fields(obj)}
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _convert(to_dict(), depth + 1)
    return _short_repr(obj)


def dumps(obj: Any) -> str:
    """JSON-encode an already-jsonable tree (bytes become a size marker)."""

    def default(o: Any) -> Any:
        if isinstance(o, bytes | bytearray | memoryview):
            return {"type": "bytes", "size": len(bytes(o))}
        return _short_repr(o)

    return json.dumps(obj, default=default, ensure_ascii=False, separators=(",", ":"))
