"""Media (images, audio, any binary) stored once by content hash, referenced from span JSON.

A media reference looks like
``{"type": "media", "sha256": "...", "mime": "image/png", "size": 123}``.
Large base64 payloads inside provider requests and responses are swapped for references so the
trace database never holds blobs (SPEC FR-30).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MEDIA_TYPE = "media"
# Base64 strings shorter than this are left inline: not worth a file.
MIN_INLINE_BYTES = 1024

_DATA_URI = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w=.-]+)*;base64,", re.ASCII)

_AUDIO_FORMATS = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "pcm": "audio/pcm",
    "pcm16": "audio/pcm",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
    "m4a": "audio/mp4",
}


class MediaStore(Protocol):
    """Where media bytes live. Local directory now; server blob store later."""

    def put(self, data: bytes, mime: str) -> dict[str, Any]: ...

    def get(self, sha256: str) -> bytes | None: ...


def sniff_mime(data: bytes) -> str:
    """Guess a MIME type from magic bytes; falls back to application/octet-stream."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data[4:8] == b"ftyp":
        return "audio/mp4" if data[8:12] in (b"M4A ", b"M4B ") else "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def media_ref(data: bytes, mime: str) -> dict[str, Any]:
    """Reference dict for bytes (does not store them)."""
    return {
        "type": MEDIA_TYPE,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime": mime,
        "size": len(data),
    }


def is_media_ref(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("type") == MEDIA_TYPE and "sha256" in obj


class LocalMediaStore:
    """Content-addressed files under a directory: ``<root>/<sha[:2]>/<sha>``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("invalid sha256")
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes, mime: str) -> dict[str, Any]:
        ref = media_ref(data, mime)
        path = self._path(ref["sha256"])
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return ref

    def get(self, sha256: str) -> bytes | None:
        path = self._path(sha256)
        return path.read_bytes() if path.exists() else None


class MemoryMediaStore:
    """In-memory store, for tests."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, data: bytes, mime: str) -> dict[str, Any]:
        ref = media_ref(data, mime)
        self.blobs[ref["sha256"]] = data
        return ref

    def get(self, sha256: str) -> bytes | None:
        return self.blobs.get(sha256)


@dataclass
class Attachment:
    """Explicit media for a span input or output: ``Attachment(data=png_bytes)`` or ``path=...``."""

    data: bytes | None = None
    path: str | Path | None = None
    mime: str | None = None

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is not None:
            return Path(self.path).read_bytes()
        raise ValueError("Attachment needs data or path")


def _b64decode(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def extract_media(obj: Any, store: MediaStore | None) -> Any:
    """Return a copy of a JSON-like tree with media swapped for references.

    Handles: raw ``bytes``, :class:`Attachment`, ``data:`` URIs, OpenAI ``input_audio`` /
    ``audio`` blocks, Anthropic ``source: {type: base64}`` blocks. With ``store=None`` references
    are still produced (bytes are dropped, never written to the database).
    """

    def put(data: bytes, mime: str | None) -> dict[str, Any]:
        mime = mime or sniff_mime(data)
        return store.put(data, mime) if store is not None else media_ref(data, mime)

    def walk(node: Any) -> Any:
        if isinstance(node, bytes | bytearray | memoryview):
            return put(bytes(node), None)
        if isinstance(node, Attachment):
            return put(node.read(), node.mime)
        if isinstance(node, str):
            m = _DATA_URI.match(node)
            if m:
                data = _b64decode(node[m.end() :])
                if data is not None:
                    return put(data, m.group("mime"))
            return node
        if isinstance(node, list | tuple):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            # Anthropic: {"type": "image", "source": {"type": "base64", "media_type", "data"}}
            src = node.get("source")
            if (
                isinstance(src, dict)
                and src.get("type") == "base64"
                and isinstance(src.get("data"), str)
            ):
                data = _b64decode(src["data"])
                if data is not None:
                    out["source"] = put(data, src.get("media_type"))
            # OpenAI: {"input_audio": {"data": b64, "format": "wav"}} and audio outputs
            for key in ("input_audio", "audio"):
                blk = node.get(key)
                if isinstance(blk, dict) and isinstance(blk.get("data"), str):
                    data = _b64decode(blk["data"])
                    if data is not None and len(data) >= MIN_INLINE_BYTES:
                        fmt = str(blk.get("format") or "").lower()
                        new = dict(out[key])
                        new["data"] = put(data, _AUDIO_FORMATS.get(fmt))
                        out[key] = new
            # Generic {"b64_json": ...} (image generation) and {"data": b64, "mime"/"media_type"}
            if isinstance(node.get("b64_json"), str):
                data = _b64decode(node["b64_json"])
                if data is not None:
                    out["b64_json"] = put(data, None)
            return out
        return node

    return walk(obj)
