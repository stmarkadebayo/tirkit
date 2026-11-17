"""Versioned prompts with deploy labels (SPEC FR-20).

A prompt is a text or chat template with ``{{variable}}`` placeholders, plus an optional model
config. Every save creates version n+1; labels such as ``production`` point at a version, so
moving a label changes what apps fetch without a redeploy. Spans created inside
``with prompt.use():`` record ``prompt_version = "name@version"``.
"""

from __future__ import annotations

import copy
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from tirkit.store import SQLStore
from tirkit.tracing import current_prompt

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class MissingVariable(KeyError):
    pass


def _lookup(variables: dict[str, Any], dotted: str) -> Any:
    cur: Any = variables
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise MissingVariable(dotted)
    return cur


def render(text: str, variables: dict[str, Any]) -> str:
    """Fill ``{{name}}`` placeholders (dotted paths allowed); a missing variable raises."""
    return _VAR.sub(lambda m: str(_lookup(variables, m.group(1))), text)


def variables_of(template: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.extend(m.group(1) for m in _VAR.finditer(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(template)
    return list(dict.fromkeys(found))


@dataclass
class Prompt:
    name: str
    version: int
    kind: str  # "text" | "chat"
    template: Any  # str, or list of {"role", "content"} messages
    config: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def variables(self) -> list[str]:
        return variables_of(self.template)

    def compile(self, **variables: Any) -> Any:
        """Rendered text, or a list of chat messages, ready for the model call."""
        if self.kind == "text":
            return render(str(self.template), variables)
        messages = copy.deepcopy(self.template)
        for m in messages:
            if isinstance(m.get("content"), str):
                m["content"] = render(m["content"], variables)
            elif isinstance(m.get("content"), list):
                for part in m["content"]:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = render(part["text"], variables)
        return messages

    @contextmanager
    def use(self) -> Iterator[Prompt]:
        """Spans started inside record this prompt version."""
        token = current_prompt.set(self.ref)
        try:
            yield self
        finally:
            current_prompt.reset(token)


def validate(kind: str, template: Any) -> None:
    if kind == "text":
        if not isinstance(template, str):
            raise ValueError("a text prompt's template must be a string")
    elif kind == "chat":
        if not isinstance(template, list) or not all(
            isinstance(m, dict) and isinstance(m.get("role"), str) and "content" in m
            for m in template
        ):
            raise ValueError("a chat prompt's template must be a list of {role, content} messages")
    else:
        raise ValueError("kind must be 'text' or 'chat'")


class PromptStore:
    """Prompt operations on a project-scoped store."""

    def __init__(self, store: SQLStore) -> None:
        self.store = store
        self.pid = store.project_id

    def create_version(
        self,
        name: str,
        template: Any,
        *,
        kind: str = "text",
        config: dict[str, Any] | None = None,
        message: str | None = None,
        labels: list[str] | None = None,
        author: str | None = None,
    ) -> int:
        validate(kind, template)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-/]{0,99}", name):
            raise ValueError("prompt names: letters, digits, _ . - / (max 100)")
        now = time.time()
        q = self.store.dialect.q
        with self.store.tx() as cur:
            cur.execute(
                q(
                    "INSERT INTO prompts (project_id, name, created) VALUES (?, ?, ?) "
                    "ON CONFLICT (project_id, name) DO NOTHING"
                ),
                (self.pid, name, now),
            )
            cur.execute(
                q("SELECT MAX(version) FROM prompt_versions WHERE project_id = ? AND name = ?"),
                (self.pid, name),
            )
            row = cur.fetchone()
            version = int(row[0] or 0) + 1
            cur.execute(
                q(
                    "INSERT INTO prompt_versions (project_id, name, version, kind, template, "
                    "config, message, created_by, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    self.pid,
                    name,
                    version,
                    kind,
                    self.store._json(template),
                    self.store._json(config or {}),
                    message,
                    author,
                    now,
                ),
            )
        for label in ["latest", *(labels or [])]:
            self.set_label(name, label, version)
        return version

    def set_label(self, name: str, label: str, version: int) -> None:
        if not _LABEL.match(label):
            raise ValueError("labels: lowercase letters, digits, - and _")
        if not self.store.execute(
            "SELECT 1 FROM prompt_versions WHERE project_id = ? AND name = ? AND version = ?",
            [self.pid, name, version],
        ):
            raise KeyError(f"no version {version} of prompt {name!r}")
        self.store.execute(
            "INSERT INTO prompt_labels (project_id, name, label, version, updated) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (project_id, name, label) DO UPDATE SET "
            "version = excluded.version, updated = excluded.updated",
            [self.pid, name, label, version, time.time()],
        )

    def labels(self, name: str) -> dict[str, int]:
        rows = self.store.execute(
            "SELECT label, version FROM prompt_labels WHERE project_id = ? AND name = ?",
            [self.pid, name],
        )
        return {r[0]: int(r[1]) for r in rows}

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        if version is None:
            labels = self.labels(name)
            want = label or "production"
            if want not in labels:
                if label is None and "latest" in labels:
                    want = "latest"  # no production label yet: fall back to the newest
                else:
                    raise KeyError(f"prompt {name!r} has no label {want!r}")
            version = labels[want]
        rows = self.store.execute(
            "SELECT kind, template, config FROM prompt_versions WHERE project_id = ? AND "
            "name = ? AND version = ?",
            [self.pid, name, version],
        )
        if not rows:
            raise KeyError(f"no version {version} of prompt {name!r}")
        load = self.store.dialect.load_json
        on_version = [lab for lab, v in self.labels(name).items() if v == version]
        return Prompt(
            name, version, rows[0][0], load(rows[0][1]), load(rows[0][2]) or {}, on_version
        )

    def versions(self, name: str) -> list[dict[str, Any]]:
        labels = self.labels(name)
        rows = self.store.execute(
            "SELECT version, kind, template, config, message, created_by, created FROM "
            "prompt_versions WHERE project_id = ? AND name = ? ORDER BY version DESC",
            [self.pid, name],
        )
        load = self.store.dialect.load_json
        return [
            {
                "version": int(r[0]),
                "kind": r[1],
                "template": load(r[2]),
                "config": load(r[3]) or {},
                "message": r[4],
                "created_by": r[5],
                "created": r[6],
                "labels": sorted(lab for lab, v in labels.items() if v == int(r[0])),
                "variables": variables_of(load(r[2])),
            }
            for r in rows
        ]

    def list(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT p.name, p.created, (SELECT MAX(version) FROM prompt_versions v WHERE "
            "v.project_id = p.project_id AND v.name = p.name) FROM prompts p "
            "WHERE p.project_id = ? ORDER BY p.name",
            [self.pid],
        )
        return [
            {
                "name": r[0],
                "created": r[1],
                "latest_version": int(r[2] or 0),
                "labels": self.labels(r[0]),
            }
            for r in rows
        ]


class _PromptCache:
    """SDK-side cache so apps do not hit the server on every request."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], tuple[float, Prompt]] = {}
        self.lock = threading.Lock()


_cache = _PromptCache()


def get_prompt(
    name: str, *, label: str | None = None, version: int | None = None, cache_ttl_s: float = 60.0
) -> Prompt:
    """Fetch a prompt by label (default ``production``, falling back to the newest) or version.

    Uses the server in server mode, else the local store. Cached for ``cache_ttl_s``; when the
    server is unreachable a cached copy is served even if stale.
    """
    from tirkit.client import get_client

    client = get_client()
    if client is None:
        raise RuntimeError("call tirkit.init() first")
    key = (name, f"v{version}" if version is not None else (label or "production"))
    now = time.time()
    with _cache.lock:
        hit = _cache.items.get(key)
    if hit and now - hit[0] < cache_ttl_s:
        return hit[1]
    try:
        if client.http is not None:
            params = {"label": label, "version": version}
            data = client.http.get(f"/api/v1/prompts/{name}", **params)
            prompt = Prompt(**data)
        else:
            prompt = PromptStore(client.store).get(name, label=label, version=version)
    except Exception:
        if hit:
            return hit[1]
        raise
    with _cache.lock:
        _cache.items[key] = (now, prompt)
    return prompt
