"""SDK entry point: ``tirkit.init()`` wires tracing to local storage or a Tirkit server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tirkit.exporter import BatchExporter
from tirkit.media import LocalMediaStore, MediaStore
from tirkit.redact import MODES
from tirkit.tracing import set_default_sink

_client: Tirkit | None = None


class Tirkit:
    """A configured Tirkit SDK client. Create it with :func:`init`."""

    def __init__(
        self,
        *,
        exporter: BatchExporter,
        store: Any = None,
        media_store: MediaStore | None = None,
        http: Any = None,
    ) -> None:
        self.exporter = exporter
        self.store = store
        self.media_store = media_store
        self.http = http

    def flush(self, timeout: float = 10.0) -> bool:
        """Write every queued span now."""
        return self.exporter.flush(timeout)

    def shutdown(self) -> None:
        self.exporter.shutdown()
        set_default_sink(None)


def init(
    *,
    path: str | Path | None = None,
    host: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    media_dir: str | Path | None = None,
    project: str | None = None,
    max_queue: int = 10_000,
    redact: str | None = None,
) -> Tirkit:
    """Start tracing.

    - Local mode (default): spans go to a SQLite file (``path``, else ``$TIRKIT_DB_PATH``, else
      ``tirkit.db``) and media to ``media_dir`` (default: ``tirkit-media`` next to the database).
    - Server mode: pass ``host`` (or set ``$TIRKIT_HOST``) plus API keys
      (``$TIRKIT_PUBLIC_KEY`` / ``$TIRKIT_SECRET_KEY``); spans are sent over HTTP.

    ``redact`` (or ``$TIRKIT_REDACT``): ``"off"``, ``"secrets"`` or ``"strict"`` redacts span
    input, output, attributes and errors in the SDK's background thread, before anything is
    written or sent. The default is no SDK-side redaction (the server applies its project's mode).
    """
    redact = redact or os.environ.get("TIRKIT_REDACT") or None
    if redact is not None and redact not in MODES:
        raise ValueError(f"redact must be one of {MODES}")
    global _client
    if _client is not None:
        _client.shutdown()
    host = host or os.environ.get("TIRKIT_HOST")
    if host:
        from tirkit.http import HttpClient

        http = HttpClient(
            host,
            public_key=public_key or os.environ.get("TIRKIT_PUBLIC_KEY", ""),
            secret_key=secret_key or os.environ.get("TIRKIT_SECRET_KEY", ""),
        )
        from tirkit.http import RemoteStore

        exporter = BatchExporter(
            http, media_store=http.media_store(), max_queue=max_queue, redact=redact
        )
        _client = Tirkit(
            exporter=exporter, store=RemoteStore(http), media_store=http.media_store(), http=http
        )
    else:
        from tirkit.store import DEFAULT_PROJECT, SQLiteStore

        db = Path(path or os.environ.get("TIRKIT_DB_PATH") or "tirkit.db")
        store = SQLiteStore(db, project_id=project or DEFAULT_PROJECT)
        store.init_schema()
        media = LocalMediaStore(media_dir or db.parent / "tirkit-media")
        exporter = BatchExporter(store, media_store=media, max_queue=max_queue, redact=redact)
        _client = Tirkit(exporter=exporter, store=store, media_store=media)
    set_default_sink(_client.exporter)
    return _client


def feedback(
    trace_id: str,
    value: float | None = None,
    *,
    name: str = "user_feedback",
    comment: str | None = None,
    value_str: str | None = None,
    span_id: str | None = None,
) -> str | None:
    """Record end-user feedback (thumbs, rating, text) as a score on a trace (SPEC FR-11).

    Use ``value=1``/``0`` for thumbs up/down, a number for ratings, ``value_str`` for categories.
    Returns the score id, or None before :func:`init`.
    """
    if _client is None:
        return None
    if _client.http is not None:
        return str(
            _client.http.score(
                trace_id, name, value, value_str=value_str, comment=comment, span_id=span_id
            )
        )
    return str(
        _client.store.save_score(
            name,
            value,
            trace_id=trace_id,
            span_id=span_id,
            value_str=value_str,
            source="user_feedback",
            reason=comment,
        )
    )


def get_client() -> Tirkit | None:
    return _client


def flush(timeout: float = 10.0) -> bool:
    """Write every queued span now (no-op before :func:`init`)."""
    return _client.flush(timeout) if _client is not None else True
