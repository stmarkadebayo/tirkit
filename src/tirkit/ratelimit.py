"""Rate limiting: token bucket, sliding window and progressive delay behind one interface.

Two backends with the same behaviour:

- :class:`MemoryBackend`: a dict in this process. Fast, but each worker process counts alone.
- :class:`DatabaseBackend`: the ``rate_limits`` table. Every operation is **one** atomic
  ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` statement, so several server processes
  sharing one database share the limits without races or extra round trips.

Algorithms:

- **Token bucket** (:class:`Rule` default): ``limit`` tokens per ``window_s`` refill continuously
  up to ``burst`` (default ``limit``). A hit costs ``cost`` tokens (for example the number of spans
  in a batch).
- **Sliding window** (``algorithm="sliding_window"``): at most ``limit`` per ``window_s``,
  estimated from the current and previous fixed windows (the previous one weighted by how much of
  it still overlaps the sliding window). Constant memory per key.
- **Progressive delay** (:class:`ProgressiveDelay`): ``free`` attempts, then each further attempt
  must wait ``base_s``, doubling up to ``cap_s``. Nothing locks out for good: the count resets after
  ``reset_after_s`` without attempts. Used for login throttling.

Times are seconds since the epoch (``time.time()``); pass ``now=`` in tests.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Algorithm = Literal["token_bucket", "sliding_window"]


@dataclass(frozen=True)
class Rule:
    """``limit`` per ``window_s`` seconds."""

    limit: float
    window_s: float
    algorithm: Algorithm = "token_bucket"
    burst: float | None = None  # token bucket capacity; defaults to ``limit``

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.limit)

    @property
    def rate(self) -> float:
        return float(self.limit) / float(self.window_s)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0  # seconds until a retry can succeed (0 when allowed)
    remaining: float = 0.0


class Backend(Protocol):
    def token_bucket(
        self, key: str, now: float, capacity: float, rate: float, cost: float
    ) -> tuple[bool, float]: ...

    def sliding_window(
        self, key: str, now: float, window_s: float, limit: float, cost: float
    ) -> tuple[bool, float, float]: ...

    def progressive(
        self, key: str, now: float, free: int, base: float, cap: float, reset_after: float
    ) -> tuple[bool, float, float, float]: ...

    def refund(self, key: str, free: int) -> None: ...

    def reset(self, key: str) -> None: ...


# -- in-process backend ------------------------------------------------------------------------


class MemoryBackend:
    """Limits held in this process (one dict, one lock)."""

    def __init__(self) -> None:
        self._state: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._ops = 0

    def _gc(self, now: float) -> None:
        self._ops += 1
        if self._ops % 10_000 == 0:
            old = now - PURGE_AFTER_S
            self._state = {k: v for k, v in self._state.items() if v[3] >= old}

    def token_bucket(
        self, key: str, now: float, capacity: float, rate: float, cost: float
    ) -> tuple[bool, float]:
        with self._lock:
            self._gc(now)
            st = self._state.get(key)
            if st is None:
                ok = cost <= capacity
                tokens = capacity - cost if ok else capacity
                self._state[key] = [tokens, 0.0, now, now]
                return ok, tokens
            refill = min(capacity, st[0] + max(0.0, now - st[2]) * rate)
            ok = refill >= cost
            st[0] = refill - cost if ok else refill
            st[2] = max(st[2], now)
            st[3] = now
            return ok, st[0]

    def sliding_window(
        self, key: str, now: float, window_s: float, limit: float, cost: float
    ) -> tuple[bool, float, float]:
        k, weight = _window(now, window_s)
        with self._lock:
            self._gc(now)
            st = self._state.get(key)
            if st is None:
                ok = cost <= limit
                self._state[key] = [cost if ok else 0.0, 0.0, k, now]
                return ok, self._state[key][0], 0.0
            curr = st[0] if st[2] == k else 0.0
            prev = st[1] if st[2] == k else st[0] if st[2] == k - 1 else 0.0
            ok = prev * weight + curr + cost <= limit
            st[0], st[1], st[2], st[3] = (curr + cost if ok else curr), prev, k, now
            return ok, st[0], prev

    def progressive(
        self, key: str, now: float, free: int, base: float, cap: float, reset_after: float
    ) -> tuple[bool, float, float, float]:
        with self._lock:
            self._gc(now)
            st = self._state.get(key)
            if st is None or now - st[2] >= reset_after:
                st = [1.0, 0.0 if 1 < free else base, now, now]
                self._state[key] = st
                return True, st[0], st[1], st[2]
            count, delay, last = st[0], st[1], st[2]
            if count < free or now - last >= delay:
                st[0] = count + 1
                st[1] = 0.0 if count + 1 < free else base if delay <= 0 else min(cap, delay * 2)
                st[2] = now
                st[3] = now
                return True, st[0], st[1], st[2]
            st[3] = now
            return False, count, delay, last

    def refund(self, key: str, free: int) -> None:
        with self._lock:
            st = self._state.get(key)
            if st is not None:
                st[0] = max(st[0] - 1, 0.0)
                if st[0] < free:
                    st[1] = 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


# -- database backend --------------------------------------------------------------------------

PURGE_AFTER_S = (
    7 * 86400
)  # rows idle this long are deleted; limits with longer windows are not supported

_PARAM = re.compile(r":([a-z_]+)")


def _bind(sql: str, **params: Any) -> tuple[str, list[Any]]:
    """Named ``:params`` to positional ``?`` (the store's dialect maps ``?`` further)."""
    values: list[Any] = []

    def sub(m: re.Match[str]) -> str:
        values.append(params[m.group(1)])
        return "?"

    return _PARAM.sub(sub, sql), values


_TOKEN_BUCKET = """
INSERT INTO rate_limits (key, a, b, ts, ok, updated)
VALUES (:key, :fresh_tokens, 0, :now, :fresh_ok, :now)
ON CONFLICT (key) DO UPDATE SET
  a = CASE WHEN {refill} >= :cost THEN {refill} - :cost ELSE {refill} END,
  ts = {MAX}(rate_limits.ts, :now),
  ok = CASE WHEN {refill} >= :cost THEN 1 ELSE 0 END,
  updated = :now
RETURNING a, ok
"""
_REFILL = "{MIN}(:capacity, rate_limits.a + {MAX}(0, :now - rate_limits.ts) * :rate)"

_SLIDING = """
INSERT INTO rate_limits (key, a, b, ts, ok, updated)
VALUES (:key, :fresh_count, 0, :k, :fresh_ok, :now)
ON CONFLICT (key) DO UPDATE SET
  a = CASE WHEN {est} + :cost <= :limit THEN {curr} + :cost ELSE {curr} END,
  b = {prev},
  ts = :k,
  ok = CASE WHEN {est} + :cost <= :limit THEN 1 ELSE 0 END,
  updated = :now
RETURNING a, b, ok
"""
_CURR = "(CASE WHEN rate_limits.ts = :k THEN rate_limits.a ELSE 0 END)"
_PREV = (
    "(CASE WHEN rate_limits.ts = :k THEN rate_limits.b "
    "WHEN rate_limits.ts = :k - 1 THEN rate_limits.a ELSE 0 END)"
)

_PROGRESSIVE = """
INSERT INTO rate_limits (key, a, b, ts, ok, updated)
VALUES (:key, 1, :first_delay, :now, 1, :now)
ON CONFLICT (key) DO UPDATE SET
  a = CASE WHEN {fresh} THEN 1 WHEN {allowed} THEN rate_limits.a + 1 ELSE rate_limits.a END,
  b = CASE WHEN {fresh} THEN :first_delay
           WHEN {allowed} THEN (CASE WHEN rate_limits.a + 1 < :free THEN 0
                                     WHEN rate_limits.b <= 0 THEN :base
                                     ELSE {MIN}(:cap, rate_limits.b * 2) END)
           ELSE rate_limits.b END,
  ts = CASE WHEN {fresh} OR {allowed} THEN :now ELSE rate_limits.ts END,
  ok = CASE WHEN {fresh} OR {allowed} THEN 1 ELSE 0 END,
  updated = :now
RETURNING a, b, ts, ok
"""
_FRESH = "(:now - rate_limits.ts >= :reset_after)"
_ALLOWED = "(rate_limits.a < :free OR :now - rate_limits.ts >= rate_limits.b)"


class DatabaseBackend:
    """Limits in the ``rate_limits`` table, shared by every process using the database."""

    def __init__(self, store: Any) -> None:
        self.store = store
        pg = store.dialect.name == "postgres"
        fn = {"MIN": "LEAST" if pg else "MIN", "MAX": "GREATEST" if pg else "MAX"}
        refill = _REFILL.format(**fn)
        self._tb = _TOKEN_BUCKET.format(refill=refill, **fn)
        self._sw = _SLIDING.format(est=f"({_PREV} * :weight + {_CURR})", curr=_CURR, prev=_PREV)
        self._pd = _PROGRESSIVE.format(fresh=_FRESH, allowed=_ALLOWED, **fn)
        self._ops = 0
        self._lock = threading.Lock()

    def _run(self, sql: str, **params: Any) -> tuple[Any, ...]:
        with self._lock:
            self._ops += 1
            purge = self._ops % 10_000 == 0
        if purge:
            self.store.execute(
                "DELETE FROM rate_limits WHERE updated < ?", [time.time() - PURGE_AFTER_S]
            )
        q, values = _bind(sql, **params)
        return self.store.execute(q, values)[0]

    def token_bucket(
        self, key: str, now: float, capacity: float, rate: float, cost: float
    ) -> tuple[bool, float]:
        fresh_ok = cost <= capacity
        a, ok = self._run(
            self._tb,
            key=key,
            now=float(now),
            capacity=float(capacity),
            rate=float(rate),
            cost=float(cost),
            fresh_tokens=float(capacity - cost if fresh_ok else capacity),
            fresh_ok=int(fresh_ok),
        )
        return bool(ok), float(a)

    def sliding_window(
        self, key: str, now: float, window_s: float, limit: float, cost: float
    ) -> tuple[bool, float, float]:
        k, weight = _window(now, window_s)
        fresh_ok = cost <= limit
        a, b, ok = self._run(
            self._sw,
            key=key,
            now=float(now),
            k=float(k),
            weight=float(weight),
            limit=float(limit),
            cost=float(cost),
            fresh_count=float(cost if fresh_ok else 0.0),
            fresh_ok=int(fresh_ok),
        )
        return bool(ok), float(a), float(b)

    def progressive(
        self, key: str, now: float, free: int, base: float, cap: float, reset_after: float
    ) -> tuple[bool, float, float, float]:
        a, b, ts, ok = self._run(
            self._pd,
            key=key,
            now=float(now),
            free=float(free),
            base=float(base),
            cap=float(cap),
            reset_after=float(reset_after),
            first_delay=float(0.0 if 1 < free else base),
        )
        return bool(ok), float(a), float(b), float(ts)

    def refund(self, key: str, free: int) -> None:
        pg = self.store.dialect.name == "postgres"
        q, values = _bind(
            f"UPDATE rate_limits SET a = {'GREATEST' if pg else 'MAX'}(a - 1, 0), "
            "b = CASE WHEN a - 1 < :free THEN 0 ELSE b END WHERE key = :key",
            free=float(free),
            key=key,
        )
        self.store.execute(q, values)

    def reset(self, key: str) -> None:
        self.store.execute("DELETE FROM rate_limits WHERE key = ?", [key])


def _window(now: float, window_s: float) -> tuple[int, float]:
    """Index of the fixed window holding ``now`` and the weight of the previous window."""
    k = math.floor(now / window_s)
    elapsed = (now - k * window_s) / window_s
    return k, 1.0 - elapsed


# -- limiters ----------------------------------------------------------------------------------


class RateLimiter:
    """A named limit: ``hit(key, cost)`` consumes and says whether the call may proceed."""

    def __init__(self, name: str, rule: Rule, backend: Backend) -> None:
        if rule.limit <= 0 or rule.window_s <= 0:
            raise ValueError("limit and window_s must be positive")
        self.name = name
        self.rule = rule
        self.backend = backend

    def hit(self, key: str, cost: float = 1.0, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        r = self.rule
        full_key = f"{self.name}:{key}"
        if r.algorithm == "sliding_window":
            ok, curr, prev = self.backend.sliding_window(full_key, now, r.window_s, r.limit, cost)
            if ok:
                return Decision(True, 0.0, max(0.0, r.limit - curr))
            return Decision(False, _sliding_retry(now, r, curr, prev, cost), 0.0)
        ok, tokens = self.backend.token_bucket(full_key, now, r.capacity, r.rate, cost)
        if ok:
            return Decision(True, 0.0, tokens)
        retry = r.window_s if cost > r.capacity else (cost - tokens) / r.rate
        return Decision(False, max(retry, 0.001), tokens)


def _sliding_retry(now: float, r: Rule, curr: float, prev: float, cost: float) -> float:
    if cost > r.limit:
        return r.window_s
    k, weight = _window(now, r.window_s)
    f = 1.0 - weight  # fraction of the current window elapsed
    if curr + cost <= r.limit and prev > 0:
        target = 1.0 - (r.limit - cost - curr) / prev
        return max((target - f) * r.window_s, 0.001)
    # Wait for the next window, where this window's count becomes the weighted previous one.
    nxt = max(0.0, 1.0 - (r.limit - cost) / curr) if curr > 0 else 0.0
    return max((1.0 - f + nxt) * r.window_s, 0.001)


class ProgressiveDelay:
    """``free`` attempts, then a delay of ``base_s`` doubling to ``cap_s`` between attempts.

    ``attempt`` counts the attempt when it is allowed (so concurrent attempts cannot slip
    through between a check and an update); call ``reset`` after a success that should clear the
    count, or ``refund`` to take back one attempt.
    """

    def __init__(
        self,
        name: str,
        *,
        free: int,
        base_s: float,
        cap_s: float,
        reset_after_s: float,
        backend: Backend,
    ) -> None:
        self.name = name
        self.free = free
        self.base_s = base_s
        self.cap_s = cap_s
        self.reset_after_s = reset_after_s
        self.backend = backend

    def attempt(self, key: str, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        ok, count, delay, last = self.backend.progressive(
            f"{self.name}:{key}", now, self.free, self.base_s, self.cap_s, self.reset_after_s
        )
        if ok:
            return Decision(True, 0.0, max(0.0, self.free - count))
        return Decision(False, max(last + delay - now, 0.001), 0.0)

    def refund(self, key: str) -> None:
        self.backend.refund(f"{self.name}:{key}", self.free)

    def reset(self, key: str) -> None:
        self.backend.reset(f"{self.name}:{key}")
