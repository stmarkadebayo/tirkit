"""Background batch exporter: keeps span writes off the hot path (SPEC FR-5, NFR-1).

``save_span`` only appends to a bounded queue. A daemon thread extracts media and writes batches to
the target (a store, or the HTTP client). When the queue is full, spans are dropped and counted,
never blocking the caller.

Fork safety: after ``os.fork()`` (directly, or through ``multiprocessing`` with the fork start
method) only the forking thread exists in the child, and any lock held by another thread stays
held forever. ``os.register_at_fork`` gives every live exporter a new lock, events, an empty queue
and a new background thread in the child. Spans queued before the fork belong to the parent,
which still exports them. ``multiprocessing`` children leave through ``os._exit`` (no ``atexit``),
so a ``multiprocessing`` finalizer flushes them on the way out.
"""

from __future__ import annotations

import atexit
import logging
import os
import random
import sys
import threading
import time
import weakref
from collections import deque
from typing import Any, Protocol

from tirkit.media import MediaStore, extract_media
from tirkit.tracing import Span

log = logging.getLogger("tirkit")


class BatchTarget(Protocol):
    def save_spans(self, spans: list[Span]) -> None: ...


def backoff_delay(
    attempt: int, *, base: float = 0.2, cap: float = 5.0, rng: random.Random | None = None
) -> float:
    """Exponential backoff with full jitter: uniform in ``[0, min(cap, base * 2**attempt)]``, so
    many clients that failed together do not retry together."""
    return (rng or random).uniform(0.0, min(cap, base * 2.0 ** min(attempt, 30)))


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
        max_retries: int = 5,
        max_retry_s: float = 30.0,
    ) -> None:
        self.target = target
        self.media_store = media_store
        self.max_queue = max_queue
        self.batch_size = batch_size
        self.interval_s = interval_s
        self.max_retries = max_retries
        self.max_retry_s = max_retry_s
        self.dropped = 0
        self.exported = 0
        self.failed = 0
        self._queue: deque[Span] = deque()
        self._stop = False
        self._start()
        _live.add(self)

    def _start(self) -> None:
        """Fresh lock, events and background thread (also used in a forked child)."""
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(target=self._run, name="tirkit-exporter", daemon=True)
        self._thread.start()

    def _after_fork_in_child(self) -> None:
        # Never touch the old lock or events here: another parent thread may have held them.
        self._queue = deque()  # the parent exports what it had queued
        self.dropped = self.exported = self.failed = 0
        if not self._stop:
            self._start()

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
        return span

    def _write(self, batch: list[Span]) -> None:
        """Write one batch, retrying failures for at most ``max_retries`` attempts and
        ``max_retry_s`` seconds. The server's ``Retry-After`` wins over the backoff; an error that
        says it is not retryable (``exc.retryable is False``, e.g. HTTP 400) fails at once."""
        batch = [self._prepare(s) for s in batch]
        deadline = time.monotonic() + self.max_retry_s
        attempt = 0
        while True:
            try:
                self.target.save_spans(batch)
                self.exported += len(batch)
                return
            except Exception as exc:  # noqa: BLE001
                wait = self._retry_wait(exc, attempt, deadline)
                if wait is None:
                    self.failed += len(batch)
                    log.warning(
                        "tirkit: failed to export %d spans: %s", len(batch), exc, exc_info=True
                    )
                    return
                time.sleep(wait)
                attempt += 1

    def _retry_wait(self, exc: Exception, attempt: int, deadline: float) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        if getattr(exc, "retryable", True) is False or attempt >= self.max_retries:
            return None
        after = getattr(exc, "retry_after", None)
        if after is not None:
            wait = float(after) + random.uniform(0.0, min(1.0, 0.1 * float(after)))
        else:
            wait = backoff_delay(attempt)
        if time.monotonic() + wait > deadline:
            return None
        return wait

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
        }


@atexit.register
def _flush_all() -> None:
    for exp in list(_live):
        try:
            exp.flush(timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


def _reinit_after_fork() -> None:
    for exp in list(_live):
        exp._after_fork_in_child()
    if "multiprocessing" in sys.modules:
        _flush_on_multiprocessing_exit()


def _flush_on_multiprocessing_exit() -> None:
    """``multiprocessing`` children skip ``atexit``; register a finalizer instead. The child
    clears its finalizer registry after this fork hook runs, so register from an after-fork
    callback, which runs once the child has finished setting up."""
    from multiprocessing import util

    def register(_: object) -> None:
        util.Finalize(None, _flush_all, exitpriority=100)

    util.register_after_fork(_FORK_TOKEN, register)


class _Token:
    pass


_FORK_TOKEN = _Token()  # weak-referenceable owner for the after-fork registration

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)
