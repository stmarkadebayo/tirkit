"""Background batch exporter: keeps span writes off the hot path (SPEC FR-5, NFR-1).

``save_span`` only appends to a bounded queue. A daemon thread extracts media and writes batches to
the target (a store, or the HTTP client). When the queue is full, spans are dropped and counted,
never blocking the caller.
"""

from __future__ import annotations

import atexit
import logging
import random
import threading
import time
import weakref
from collections import deque
from typing import Any, Protocol

from tirkit.media import MediaStore, extract_media
from tirkit.redact import MODES, SDK_REDACTIONS_ATTR, Redactor
from tirkit.tracing import Span

log = logging.getLogger("tirkit")

MAX_RATE_LIMIT_WAIT_S = 60.0  # longest total wait on 429 responses for one batch


class BatchTarget(Protocol):
    def save_spans(self, spans: list[Span]) -> None: ...


_live: weakref.WeakSet[BatchExporter] = weakref.WeakSet()


class BatchExporter:
    """Queue spans and write them in batches from a background thread."""

    def __init__(
        self,
        target: BatchTarget,
        *,
        media_store: MediaStore | None = None,
        max_queue: int = 10_000,
        batch_size: int = 256,
        interval_s: float = 0.25,
        max_retries: int = 3,
        redact: str | None = None,
        redact_patterns: list[tuple[str, str]] | None = None,
    ) -> None:
        if redact is not None and redact not in MODES:
            raise ValueError(f"redact must be one of {MODES}")
        self.redact = redact
        self.redact_patterns = list(redact_patterns or [])
        self.redacted = 0
        self.target = target
        self.media_store = media_store
        self.max_queue = max_queue
        self.batch_size = batch_size
        self.interval_s = interval_s
        self.max_retries = max_retries
        self.dropped = 0
        self.exported = 0
        self.failed = 0
        self.throttled = 0  # times a batch waited for a 429 Retry-After
        self._queue: deque[Span] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="tirkit-exporter", daemon=True)
        self._thread.start()
        _live.add(self)

    # Hot path ---------------------------------------------------------------------------------

    def save_span(self, span: Span) -> None:
        with self._lock:
            if len(self._queue) >= self.max_queue:
                self.dropped += 1
                if self.dropped in (1, 100, 10_000) or self.dropped % 100_000 == 0:
                    log.warning("tirkit: export queue full, %d spans dropped", self.dropped)
                return
            self._queue.append(span)
            self._idle.clear()
            size = len(self._queue)
        if size >= self.batch_size:
            self._wake.set()

    # Background -------------------------------------------------------------------------------

    def _take(self) -> list[Span]:
        with self._lock:
            n = min(len(self._queue), self.batch_size)
            return [self._queue.popleft() for _ in range(n)]

    def _prepare(self, span: Span) -> Span:
        try:
            span.input = extract_media(span.input, self.media_store)
            span.output = extract_media(span.output, self.media_store)
            if span.attributes:
                span.attributes = extract_media(span.attributes, self.media_store)
        except Exception:  # noqa: BLE001
            log.warning("tirkit: media extraction failed for span %s", span.name, exc_info=True)
        if self.redact and self.redact != "off":
            # Runs here, in the exporter thread, so redaction never slows the traced code.
            r = Redactor(self.redact, self.redact_patterns)
            span.input = r.obj(span.input)
            span.output = r.obj(span.output)
            span.attributes = r.obj(span.attributes) or {}
            span.error = r.text(span.error) if span.error else span.error
            if r.count:
                span.attributes[SDK_REDACTIONS_ATTR] = r.count
                self.redacted += r.count
        return span

    def _write(self, batch: list[Span]) -> None:
        batch = [self._prepare(s) for s in batch]
        delay = 0.2
        # A 429 is the server asking us to slow down, not a failed batch: wait as long as it says
        # (up to MAX_RATE_LIMIT_WAIT_S in total) without using up the normal retries.
        patience = time.monotonic() + MAX_RATE_LIMIT_WAIT_S
        attempt = 0
        while True:
            try:
                self.target.save_spans(batch)
                self.exported += len(batch)
                return
            except Exception as exc:  # noqa: BLE001
                asked = getattr(exc, "retry_after", None)
                if asked is not None:
                    wait = min(float(asked), 30.0) * (1 + 0.1 * random.random())
                    if time.monotonic() + wait <= patience:
                        self.throttled += 1
                        time.sleep(wait)
                        continue
                if attempt >= self.max_retries or asked is not None:
                    self.failed += len(batch)
                    log.warning("tirkit: failed to export %d spans", len(batch), exc_info=True)
                    return
                attempt += 1
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def _run(self) -> None:
        while True:
            self._wake.wait(self.interval_s)
            self._wake.clear()
            while True:
                batch = self._take()
                if not batch:
                    break
                self._write(batch)
            with self._lock:
                if not self._queue:
                    self._idle.set()
            if self._stop:
                return

    # Control ----------------------------------------------------------------------------------

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything queued so far is written. Returns False on timeout."""
        self._wake.set()
        return self._idle.wait(timeout)

    def shutdown(self, timeout: float = 10.0) -> None:
        self.flush(timeout)
        self._stop = True
        self._wake.set()
        self._thread.join(timeout)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            queued = len(self._queue)
        return {
            "queued": queued,
            "exported": self.exported,
            "dropped": self.dropped,
            "failed": self.failed,
            "throttled": self.throttled,
            "redacted": self.redacted,
        }


@atexit.register
def _flush_all() -> None:
    for exp in list(_live):
        try:
            exp.flush(timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
