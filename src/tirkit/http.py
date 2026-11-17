"""SDK transport to a Tirkit server: span batches, media uploads, scores and prompts over HTTP."""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from tirkit.media import media_ref
from tirkit.serialize import dumps
from tirkit.types import Dataset, Example, RunResult, Span, Trace


class RateLimited(Exception):
    """The server answered 429; ``retry_after`` is how many seconds it asked us to wait."""

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(f"rate limited, retry after {retry_after:g}s {detail}".strip())
        self.retry_after = retry_after


def _retry_after(response: Any) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after", 1)))
    except (TypeError, ValueError):
        return 1.0


class HttpClient:
    """Talks to a Tirkit server with a project API key (HTTP Basic: public key, secret key)."""

    def __init__(self, host: str, *, public_key: str, secret_key: str, timeout: float = 30) -> None:
        import httpx

        self.host = host.rstrip("/")
        self.client = httpx.Client(
            base_url=self.host, auth=(public_key, secret_key), timeout=timeout
        )

    def save_spans(self, spans: list[Span]) -> None:
        body = dumps({"spans": [s.to_dict() for s in spans]})
        r = self.client.post(
            "/api/v1/ingest", content=body, headers={"content-type": "application/json"}
        )
        if r.status_code == 429:
            raise RateLimited(_retry_after(r))
        r.raise_for_status()

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
