"""SDK transport to a Tirkit server: span batches, media uploads, scores and prompts over HTTP."""

from __future__ import annotations

import hashlib
import os
import threading
import weakref
from typing import Any

from tirkit.media import media_ref
from tirkit.serialize import dumps
from tirkit.types import Dataset, Example, RunResult, Span, Trace

# Statuses worth another attempt: rate limited, timed out, or the server or a proxy in front of it
# is briefly unavailable. Ingest is an idempotent upsert, so re-sending a batch is safe.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ExportHTTPError(Exception):
    """A span batch the server did not accept. ``retryable`` tells the exporter whether to try
    again; ``retry_after`` is the server's ``Retry-After`` in seconds, when it sent one."""

    retryable = False

    def __init__(self, status: int | None, retry_after: float | None = None, msg: str = "") -> None:
        super().__init__(msg or f"ingest failed with HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class RetryableHTTPError(ExportHTTPError):
    """429, 5xx from a proxy or overloaded server, timeouts and connection errors."""

    retryable = True


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delay-seconds or an HTTP date)."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        import email.utils
        import time

        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


class HttpClient:
    """Talks to a Tirkit server with a project API key (HTTP Basic: public key, secret key)."""

    def __init__(self, host: str, *, public_key: str, secret_key: str, timeout: float = 30) -> None:
        import httpx

        self.host = host.rstrip("/")
        self._auth = (public_key, secret_key)
        self._timeout = timeout
        self.client = httpx.Client(base_url=self.host, auth=self._auth, timeout=timeout)
        # Resolved now, in this process: on macOS the system proxy lookup is not safe to call
        # in a forked child once the parent has used it (the child crashes).
        self._proxy = _proxy_for(self.host)
        _http_clients.add(self)

    def _after_fork_in_child(self) -> None:
        """The inherited pool's sockets (and maybe its locks) are shared with the parent; a child
        gets its own pool. The old client is dropped without closing it."""
        import httpx

        _inherited_clients.append(self.client)
        self.client = httpx.Client(
            base_url=self.host,
            auth=self._auth,
            timeout=self._timeout,
            trust_env=False,  # no system proxy lookup here; proxy and CA env resolved below
            proxy=self._proxy,
            verify=httpx.create_ssl_context(trust_env=True),
        )

    def save_spans(self, spans: list[Span]) -> None:
        """Send one batch. Raises :class:`RetryableHTTPError` (with the server's ``Retry-After``)
        for failures worth retrying and :class:`ExportHTTPError` for the rest; the exporter
        decides when to retry."""
        import httpx

        body = dumps({"spans": [s.to_dict() for s in spans]})
        try:
            r = self.client.post(
                "/api/v1/ingest", content=body, headers={"content-type": "application/json"}
            )
        except httpx.TransportError as exc:
            raise RetryableHTTPError(None, msg=f"ingest failed: {exc!r}") from exc
        if r.is_success:
            return
        retry_after = parse_retry_after(r.headers.get("retry-after"))
        if r.status_code in RETRY_STATUSES:
            raise RetryableHTTPError(r.status_code, retry_after)
        raise ExportHTTPError(r.status_code, msg=f"ingest failed with HTTP {r.status_code}")

    def save_span(self, span: Span) -> None:
        self.save_spans([span])

    def score(
        self,
        trace_id: str,
        name: str,
        value: float | None = None,
        *,
        value_str: str | None = None,
        comment: str | None = None,
        source: str = "user_feedback",
        span_id: str | None = None,
    ) -> str:
        r = self.client.post(
            "/api/v1/scores",
            json={
                "trace_id": trace_id,
                "span_id": span_id,
                "name": name,
                "value": value,
                "value_str": value_str,
                "comment": comment,
                "source": source,
            },
        )
        r.raise_for_status()
        return str(r.json()["score_id"])

    def get(self, path: str, **params: Any) -> Any:
        r = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def media_store(self) -> HttpMediaStore:
        return HttpMediaStore(self)


def _proxy_for(url: str) -> str | None:
    """The proxy httpx would use for ``url`` from the environment or system settings."""
    from urllib.parse import urlsplit
    from urllib.request import getproxies, proxy_bypass

    parts = urlsplit(url)
    proxies = getproxies()
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    if not proxy or (parts.hostname and proxy_bypass(parts.hostname)):
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


_http_clients: weakref.WeakSet[HttpClient] = weakref.WeakSet()
_inherited_clients: list[Any] = []


def _reset_after_fork() -> None:
    for hc in list(_http_clients):
        hc._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


class HttpMediaStore:
    """Uploads media once per content hash (skipped when the server already has it)."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def put(self, data: bytes, mime: str) -> dict[str, Any]:
        ref = media_ref(data, mime)
        sha = ref["sha256"]
        with self._lock:
            if sha in self._known:
                return ref
        r = self.http.client.put(
            f"/api/v1/media/{sha}", content=data, headers={"content-type": mime}
        )
        r.raise_for_status()
        with self._lock:
            self._known.add(sha)
        return ref

    def get(self, sha256: str) -> bytes | None:
        r = self.http.client.get(f"/api/v1/media/{sha256}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() != sha256:
            raise ValueError("media content does not match its hash")
        return bytes(r.content)


class RemoteStore:
    """The parts of the store API that runs and the CLI need, backed by a Tirkit server, so
    ``run(..., store=client.store)`` puts datasets and experiments in the server's UI."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.project_id: str = http.get("/api/v1/project")["project_id"]

    def _path(self, rest: str) -> str:
        return f"/api/projects/{self.project_id}{rest}"

    def save_dataset(self, ds: Any, description: str | None = None, note: str | None = None) -> str:
        from dataclasses import asdict

        r = self.http.client.put(
            self._path(f"/datasets/{ds.name}"),
            content=dumps(
                {
                    "items": [asdict(e) for e in ds.examples],
                    "description": description,
                    "note": note,
                }
            ),
            headers={"content-type": "application/json"},
        )
        r.raise_for_status()
        return str(r.json()["version"])

    def load_dataset(self, name: str, version: str | None = None) -> Any:
        r = self.http.client.get(
            self._path(f"/datasets/{name}"), params={"version": version} if version else None
        )
        if r.status_code == 404:
            raise KeyError(f"no dataset named {name!r}")
        r.raise_for_status()
        data = r.json()
        return Dataset(
            name,
            [
                Example(i["input"], i.get("expected"), i.get("metadata") or {})
                for i in data["items"]
            ],
        )

    def get_trace(self, trace_id: str) -> Any:
        r = self.http.client.get(self._path(f"/traces/{trace_id}"))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        t = r.json()["trace"]
        return Trace(**t)

    def save_experiment(self, result: Any) -> None:
        r = self.http.client.post(
            self._path("/experiments"),
            content=dumps(result.to_json()),
            headers={"content-type": "application/json"},
        )
        r.raise_for_status()

    def load_experiment(self, experiment_id: str) -> Any:
        r = self.http.client.get(self._path(f"/experiments/{experiment_id}"))
        if r.status_code == 404:
            raise KeyError(f"no experiment {experiment_id!r}")
        r.raise_for_status()
        return RunResult.from_json(r.json())

    def list_datasets(self) -> list[dict[str, Any]]:
        return list(self.http.get(self._path("/datasets")))

    def cache_get(self, key: str) -> Any:
        return None  # judge cache is local to the process in server mode

    def cache_put(self, key: str, result: Any) -> None:
        return None
