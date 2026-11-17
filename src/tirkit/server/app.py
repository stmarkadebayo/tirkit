"""FastAPI application (SPEC FR-22–FR-26).

Two ways to authenticate:

- **API keys** (SDKs, OTLP exporters, CI): HTTP Basic with the project's public key as the user
  name and the secret key as the password. A key can only touch its own project.
- **Sessions** (web UI): login sets an HttpOnly session cookie plus a CSRF token; every
  state-changing request made with a session must send ``X-CSRF-Token``.

Every project-scoped query goes through a store view bound to that project.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from tirkit import pricing
from tirkit.accounts import ROLES, Accounts, User
from tirkit.media import LocalMediaStore
from tirkit.redact import redact_obj
from tirkit.serialize import dumps
from tirkit.server.settings import Settings
from tirkit.store import SQLStore, open_store
from tirkit.tracing import Span

SESSION_COOKIE = "tirkit_session"
_ROLE_RANK = {r: i for i, r in enumerate(reversed(ROLES))}  # viewer 0, member 1, admin 2
_SHA = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Ctx:
    """Who is calling and for which project."""

    store: SQLStore
    project_id: str
    role: str
    user: User | None = None
    via: str = "session"

    def require(self, role: str) -> None:
        if _ROLE_RANK[self.role] < _ROLE_RANK[role]:
            raise HTTPException(403, f"requires {role} role")


def _json(data: Any, status: int = 200) -> Response:
    return Response(dumps(data), status_code=status, media_type="application/json")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = open_store(settings.database_url)
    store.init_schema()
    accounts = Accounts(store)
    media = LocalMediaStore(settings.media_dir)
    if settings.admin_email and settings.admin_password and accounts.count_users() == 0:
        admin = accounts.create_user(settings.admin_email, settings.admin_password, is_admin=True)
        accounts.create_project("Default", admin.user_id)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        worker = None
        if settings.embedded_worker:
            from tirkit.jobs import Worker

            worker = Worker(store)
            worker.start_thread()
        yield
        if worker is not None:
            worker.stop()

    app = FastAPI(
        title="Tirkit",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.accounts = accounts
    app.state.settings = settings

    # -- auth helpers ----------------------------------------------------------------------------

    def _basic(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                pk, _, sk = base64.b64decode(header[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            return pk, sk
        if header.lower().startswith("bearer ") and ":" in header[7:]:
            pk, _, sk = header[7:].partition(":")
            return pk, sk
        return None

    def key_project(request: Request) -> str:
        creds = _basic(request)
        project_id = accounts.verify_api_key(*creds) if creds else None
        if not project_id:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Basic"})
        return project_id

    def session(request: Request) -> tuple[User, str]:
        token = request.cookies.get(SESSION_COOKIE)
        found = accounts.session_user(token) if token else None
        if not found:
            raise HTTPException(401, "not signed in")
        user, csrf = found
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-csrf-token") != csrf:
                raise HTTPException(403, "missing or invalid CSRF token")
        return user, csrf

    def project_ctx(project_id: str, request: Request) -> Ctx:
        creds = _basic(request)
        if creds:
            key_pid = accounts.verify_api_key(*creds)
            if key_pid != project_id:
                raise HTTPException(401 if key_pid is None else 403, "API key not valid here")
            return Ctx(store.for_project(project_id), project_id, "member", via="key")
        user, _ = session(request)
        role = accounts.role(user, project_id)
        if role is None:
            raise HTTPException(404, "project not found")
        return Ctx(store.for_project(project_id), project_id, role, user)

    def set_session_cookies(resp: Response, token: str, csrf: str) -> None:
        opts = {"secure": settings.secure_cookies, "samesite": "lax", "path": "/"}
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=14 * 86400, **opts)  # type: ignore[arg-type]
        resp.set_cookie("tirkit_csrf", csrf, httponly=False, max_age=14 * 86400, **opts)  # type: ignore[arg-type]

    _cache: dict[tuple[str, str], tuple[float, Any]] = {}

    def _cached(kind: str, pid: str, load: Callable[[], Any], ttl: float = 5.0) -> Any:
        """Short TTL cache for per-project settings and prices on the ingestion hot path."""
        hit = _cache.get((kind, pid))
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = load()
        _cache[(kind, pid)] = (now, value)
        return value

    def project_settings(pid: str, fresh: bool = False) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            return (accounts.project(pid) or {}).get("settings") or {}

        return load() if fresh else _cached("settings", pid, load)

    # -- health and setup ------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        store.execute("SELECT 1")
        return {"ok": True, "dialect": store.dialect.name}

    @app.get("/api/setup")
    def setup_status() -> dict[str, bool]:
        return {"needs_setup": accounts.count_users() == 0}

    @app.post("/api/setup")
    async def setup(request: Request) -> Response:
        if accounts.count_users() > 0:
            raise HTTPException(409, "already set up")
        body = await request.json()
        try:
            user = accounts.create_user(
                body.get("email", ""),
                body.get("password", ""),
                name=body.get("name"),
                is_admin=True,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        pid = accounts.create_project(body.get("project_name") or "Default", user.user_id)
        token, csrf = accounts.create_session(user.user_id)
        resp = _json({"user": asdict(user), "project_id": pid})
        set_session_cookies(resp, token, csrf)
        return resp

    @app.post("/api/auth/login")
    async def login(request: Request) -> Response:
        body = await request.json()
        user = accounts.authenticate(body.get("email", ""), body.get("password", ""))
        if not user:
            raise HTTPException(401, "wrong email or password")
        token, csrf = accounts.create_session(user.user_id)
        resp = _json({"user": asdict(user)})
        set_session_cookies(resp, token, csrf)
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request, s: tuple[User, str] = Depends(session)) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            accounts.delete_session(token)
        resp = _json({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie("tirkit_csrf", path="/")
        return resp

    @app.get("/api/auth/me")
    def me(s: tuple[User, str] = Depends(session)) -> Response:
        user, csrf = s
        return _json({"user": asdict(user), "csrf": csrf, "projects": accounts.projects_for(user)})

    # -- projects, members, keys, settings --------------------------------------------------------

    @app.post("/api/projects")
    async def create_project(request: Request, s: tuple[User, str] = Depends(session)) -> Response:
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        return _json({"project_id": accounts.create_project(name, s[0].user_id)}, 201)

    @app.get("/api/projects/{project_id}")
    def get_project(ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json({**(accounts.project(ctx.project_id) or {}), "role": ctx.role})

    @app.put("/api/projects/{project_id}/settings")
    async def put_settings(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        body = await request.json()
        allowed = {
            "redact",
            "redact_patterns",
            "retention_days",
            "judge_budget_usd_per_day",
            "name",
        }
        current = project_settings(ctx.project_id, fresh=True)
        settings_ = {**current, **{k: v for k, v in body.items() if k in allowed}}
        for pat in settings_.get("redact_patterns") or []:
            try:
                re.compile(pat["pattern"])
            except (re.error, KeyError, TypeError) as exc:
                raise HTTPException(400, f"bad redaction pattern: {exc}") from exc
        accounts.update_settings(ctx.project_id, settings_)
        _cache.pop(("settings", ctx.project_id), None)
        return _json(settings_)

    @app.get("/api/projects/{project_id}/members")
    def list_members(ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(accounts.members(ctx.project_id))

    @app.post("/api/projects/{project_id}/members")
    async def add_member(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        body = await request.json()
        user = accounts.find_user(body.get("email", ""))
        if user is None:
            if not body.get("password"):
                raise HTTPException(404, "no such user; pass a password to create one")
            try:
                user = accounts.create_user(body["email"], body["password"], name=body.get("name"))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        try:
            accounts.set_role(user.user_id, ctx.project_id, body.get("role", "member"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _json({"user_id": user.user_id}, 201)

    @app.delete("/api/projects/{project_id}/members/{user_id}")
    def remove_member(user_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        accounts.remove_member(user_id, ctx.project_id)
        return _json({"ok": True})

    @app.get("/api/projects/{project_id}/keys")
    def list_keys(ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        return _json(accounts.list_api_keys(ctx.project_id))

    @app.post("/api/projects/{project_id}/keys")
    async def create_key(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        body = await request.json() if await request.body() else {}
        pk, sk = accounts.create_api_key(ctx.project_id, body.get("name"))
        return _json({"public_key": pk, "secret_key": sk}, 201)

    @app.delete("/api/projects/{project_id}/keys/{public_key}")
    def delete_key(public_key: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        accounts.delete_api_key(ctx.project_id, public_key)
        return _json({"ok": True})

    @app.get("/api/projects/{project_id}/prices")
    def get_prices(ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(ctx.store.get_prices())

    @app.put("/api/projects/{project_id}/prices/{model:path}")
    async def put_price(model: str, request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        body = await request.json()
        prices = {k: float(v) for k, v in body.items()} if body else None
        if prices and set(prices) - set(pricing.UNITS):
            raise HTTPException(400, f"unknown units: {sorted(set(prices) - set(pricing.UNITS))}")
        ctx.store.set_price(model, prices)
        _cache.pop(("prices", ctx.project_id), None)
        return _json({"model": model, "prices": prices})

    # -- ingestion ---------------------------------------------------------------------------------

    def _prepare(spans: list[Span], pid: str) -> list[Span]:
        st = project_settings(pid)
        overrides = _cached("prices", pid, store.for_project(pid).get_prices)
        extra = [
            (p["pattern"], p.get("label", "[REDACTED]")) for p in st.get("redact_patterns") or []
        ]
        for s in spans:
            if not s.trace_id or not s.span_id:
                raise HTTPException(400, "every span needs trace_id and span_id")
            if s.cost_usd is None and s.usage:
                s.cost_usd = pricing.cost(s.model, s.usage, overrides)
            if st.get("redact"):
                s.input = redact_obj(s.input, extra)
                s.output = redact_obj(s.output, extra)
                s.attributes = redact_obj(s.attributes, extra)
        return spans

    async def _body(request: Request, limit: int) -> bytes:
        size = int(request.headers.get("content-length") or 0)
        if size > limit:
            raise HTTPException(413, "request too large")
        body = await request.body()
        if len(body) > limit:
            raise HTTPException(413, "request too large")
        return body

    @app.post("/api/v1/ingest")
    async def ingest(request: Request, pid: str = Depends(key_project)) -> Response:
        body = await _body(request, settings.max_ingest_bytes)
        try:
            data = json.loads(body)
            spans = [Span.from_dict(d) for d in data.get("spans", [])]
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(400, f"invalid payload: {exc}") from exc
        store.for_project(pid).save_spans(_prepare(spans, pid))
        return _json({"accepted": len(spans)})

    @app.post("/v1/traces")
    async def otlp_traces(request: Request, pid: str = Depends(key_project)) -> Response:
        from tirkit.otel import decode_request

        body = await _body(request, settings.max_ingest_bytes)
        ctype = request.headers.get("content-type", "application/x-protobuf")
        try:
            spans = decode_request(body, ctype)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid OTLP payload: {exc}") from exc
        store.for_project(pid).save_spans(_prepare(spans, pid))
        if "json" in ctype:
            return Response("{}", media_type="application/json")
        return Response(b"", media_type="application/x-protobuf")

    @app.put("/api/v1/media/{sha256}")
    async def put_media(sha256: str, request: Request, pid: str = Depends(key_project)) -> Response:
        if not _SHA.match(sha256):
            raise HTTPException(400, "bad hash")
        data = await _body(request, settings.max_media_bytes)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise HTTPException(400, "content does not match hash")
        mime = request.headers.get("content-type") or "application/octet-stream"
        media.put(data, mime)
        store.for_project(pid).record_media(sha256, mime, len(data))
        return _json({"sha256": sha256, "size": len(data)}, 201)

    def _serve_media(st: SQLStore, sha256: str) -> Response:
        if not _SHA.match(sha256):
            raise HTTPException(400, "bad hash")
        info = st.media_info(sha256)
        data = media.get(sha256) if info else None
        if data is None or info is None:
            raise HTTPException(404, "media not found")
        return Response(
            data,
            media_type=info["mime"] or "application/octet-stream",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, max-age=86400, immutable",
            },
        )

    @app.get("/api/v1/media/{sha256}")
    def get_media_key(sha256: str, pid: str = Depends(key_project)) -> Response:
        return _serve_media(store.for_project(pid), sha256)

    @app.get("/api/projects/{project_id}/media/{sha256}")
    def get_media(sha256: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        return _serve_media(ctx.store, sha256)

    @app.post("/api/v1/scores")
    async def post_score_key(request: Request, pid: str = Depends(key_project)) -> Response:
        return _save_score(store.for_project(pid), await request.json(), None, "api")

    def _save_score(
        st: SQLStore, body: dict[str, Any], author: str | None, default_source: str
    ) -> Response:
        if not body.get("name"):
            raise HTTPException(400, "name required")
        if body.get("value") is None and body.get("value_str") is None:
            raise HTTPException(400, "value or value_str required")
        if not body.get("trace_id"):
            raise HTTPException(400, "trace_id required")
        source = body.get("source") or default_source
        if source not in {"api", "user_feedback", "human", "llm_judge", "rule"}:
            raise HTTPException(400, "unknown source")
        sid = st.save_score(
            str(body["name"]),
            None if body.get("value") is None else float(body["value"]),
            trace_id=body.get("trace_id"),
            span_id=body.get("span_id"),
            value_str=body.get("value_str"),
            source=source,
            reason=body.get("comment"),
            author=author,
        )
        return _json({"score_id": sid}, 201)

    @app.post("/api/projects/{project_id}/scores")
    async def post_score(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        author = ctx.user.email if ctx.user else None
        return _save_score(ctx.store, await request.json(), author, "human")

    # -- reading traces ---------------------------------------------------------------------------

    def _filters(request: Request) -> dict[str, Any]:
        q = request.query_params
        where: dict[str, Any] = {}
        for key in ("name", "user_id", "session_id", "release", "tag", "search", "model", "kind"):
            if q.get(key):
                where[key] = q[key]
        for key in ("start_after", "start_before", "min_cost", "min_duration_ms"):
            if q.get(key):
                where[key] = float(q[key])
        if q.get("error") in ("true", "false"):
            where["error"] = q["error"] == "true"
        return where

    @app.get("/api/projects/{project_id}/traces")
    def list_traces(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        where = _filters(request)
        for k in ("model", "kind", "tag"):
            where.pop(k, None)  # span-level filters are not on the traces table
        limit = min(int(request.query_params.get("limit", 50)), 500)
        offset = int(request.query_params.get("offset", 0))
        items = ctx.store.query_traces(where, limit=limit, offset=offset)
        total = (
            ctx.store.count_traces(where) if request.query_params.get("count") != "false" else None
        )
        return _json({"items": [asdict(t) for t in items], "total": total})

    @app.get("/api/projects/{project_id}/traces/{trace_id}")
    def get_trace(trace_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        trace = ctx.store.get_trace(trace_id)
        if trace is None:
            raise HTTPException(404, "trace not found")
        return _json(
            {
                "trace": asdict(trace),
                "spans": [s.to_dict() for s in ctx.store.get_spans(trace_id)],
                "scores": ctx.store.query_scores(trace_id=trace_id),
            }
        )

    @app.get("/api/projects/{project_id}/spans")
    def list_spans(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        limit = min(int(request.query_params.get("limit", 50)), 500)
        offset = int(request.query_params.get("offset", 0))
        spans = ctx.store.query_spans(_filters(request), limit=limit, offset=offset)
        return _json({"items": [s.to_dict() for s in spans]})

    @app.get("/api/projects/{project_id}/sessions")
    def list_sessions(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(
            ctx.store.sessions(limit=min(int(request.query_params.get("limit", 100)), 500))
        )

    @app.get("/api/projects/{project_id}/sessions/{session_id}")
    def get_session(session_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        traces = ctx.store.query_traces({"session_id": session_id}, limit=500)
        traces.sort(key=lambda t: t.start or 0)
        return _json({"session_id": session_id, "traces": [asdict(t) for t in traces]})

    @app.get("/api/projects/{project_id}/users")
    def list_users(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(ctx.store.users(limit=min(int(request.query_params.get("limit", 100)), 500)))

    @app.get("/api/projects/{project_id}/filters")
    def filters(ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(ctx.store.filter_values())

    @app.get("/api/projects/{project_id}/metrics")
    def metrics(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        q = request.query_params
        until = float(q.get("until") or time.time())
        since = float(q.get("since") or until - 24 * 3600)
        buckets = max(1, min(int(q.get("buckets", 48)), 500))
        return _json(ctx.store.metrics(since, until, buckets=buckets, group_by=q.get("group_by")))

    @app.get("/api/projects/{project_id}/export/traces.jsonl")
    def export_traces(request: Request, ctx: Ctx = Depends(project_ctx)) -> StreamingResponse:
        where = _filters(request)

        def rows() -> Any:
            offset = 0
            while True:
                batch = ctx.store.query_spans(where, limit=1000, offset=offset, order="asc")
                if not batch:
                    return
                for s in batch:
                    yield dumps(s.to_dict()) + "\n"
                offset += len(batch)

        return StreamingResponse(
            rows(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=traces.jsonl"},
        )

    # -- datasets and experiments --------------------------------------------------------------

    @app.get("/api/projects/{project_id}/datasets")
    def list_datasets(ctx: Ctx = Depends(project_ctx)) -> Response:
        return _json(ctx.store.list_datasets())

    @app.get("/api/projects/{project_id}/datasets/{name}")
    def get_dataset(name: str, request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        try:
            ds = ctx.store.load_dataset(name, request.query_params.get("version"))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _json(
            {
                "name": ds.name,
                "version": ds.version,
                "items": [asdict(e) for e in ds.examples],
                "versions": ctx.store.dataset_versions(name),
            }
        )

    @app.post("/api/projects/{project_id}/datasets/from-traces")
    async def dataset_from_traces(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        from tirkit.datasets import from_traces

        ctx.require("member")
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        where = {k: v for k, v in (body.get("filter") or {}).items() if v not in (None, "")}
        if body.get("trace_ids"):
            spans = [
                s
                for tid in body["trace_ids"][:5000]
                for s in ctx.store.get_spans(tid)
                if s.parent_id is None
            ]
        else:
            where.setdefault("root", True)
            spans = ctx.store.query_spans(where, limit=int(body.get("limit", 1000)))
        ds = from_traces(
            spans,
            name=name,
            expected_from_output=bool(body.get("expected_from_output")),
            threshold=float(body.get("threshold", 0.9)),
        )
        if body.get("append"):
            try:
                old = ctx.store.load_dataset(name)
                from tirkit.datasets import Dataset, remove_duplicates

                ds = Dataset(name, remove_duplicates([*old.examples, *ds.examples]))
            except KeyError:
                pass
        version = ctx.store.save_dataset(ds, description=body.get("description"))
        return _json({"name": name, "version": version, "n_items": len(ds)}, 201)

    @app.put("/api/projects/{project_id}/datasets/{name}")
    async def put_dataset(name: str, request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        """Replace a dataset's items (creates a new version; old versions stay readable)."""
        from tirkit.datasets import Dataset, Example

        ctx.require("member")
        body = await request.json()
        items = body.get("items")
        if not isinstance(items, list):
            raise HTTPException(400, "items must be a list")
        try:
            examples = [
                Example(
                    i["input"] if isinstance(i["input"], dict) else {"input": i["input"]},
                    i.get("expected"),
                    i.get("metadata") or {},
                )
                for i in items
            ]
        except (KeyError, TypeError) as exc:
            raise HTTPException(400, "each item needs an input") from exc
        version = ctx.store.save_dataset(
            Dataset(name, examples), description=body.get("description"), note=body.get("note")
        )
        return _json({"name": name, "version": version, "n_items": len(examples)})

    @app.post("/api/projects/{project_id}/datasets/{name}/upload")
    async def upload_dataset(
        name: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        """Body: JSONL (one example per line)."""
        from tirkit.datasets import Dataset, _example_from_row

        ctx.require("member")
        text = (await _body(request, settings.max_ingest_bytes)).decode("utf-8")
        try:
            examples = [
                _example_from_row(json.loads(line)) for line in text.splitlines() if line.strip()
            ]
        except ValueError as exc:
            raise HTTPException(400, f"invalid JSONL: {exc}") from exc
        version = ctx.store.save_dataset(Dataset(name, examples))
        return _json({"name": name, "version": version, "n_items": len(examples)}, 201)

    @app.get("/api/v1/project")
    def key_whoami(pid: str = Depends(key_project)) -> Response:
        proj = accounts.project(pid) or {}
        return _json({"project_id": pid, "name": proj.get("name")})

    @app.post("/api/projects/{project_id}/experiments")
    async def post_experiment(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        """Store a finished run (``RunResult.to_json()``) from the SDK or CLI."""
        from tirkit.runner import RunResult

        ctx.require("member")
        try:
            result = RunResult.from_json(await request.json())
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid run: {exc}") from exc
        ctx.store.save_experiment(result)
        return _json({"experiment_id": result.experiment_id}, 201)

    @app.get("/api/projects/{project_id}/experiments")
    def list_experiments(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        rows = ctx.store.list_experiments(request.query_params.get("dataset"))
        out = []
        for r in rows:
            try:
                res = ctx.store.load_experiment(r["experiment_id"])
                r["summary"] = res.summary(n_resamples=2000)
                r["n_items"] = len(res.outputs)
            except KeyError:
                pass
            out.append(r)
        return _json(out)

    @app.get("/api/projects/{project_id}/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        try:
            res = ctx.store.load_experiment(experiment_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            ds = ctx.store.load_dataset(res.dataset, res.dataset_version)
            inputs = [asdict(e) for e in ds.examples]
        except (KeyError, ValueError):
            inputs = []
        return _json({**res.to_json(), "summary": res.summary(), "examples": inputs})

    @app.get("/api/projects/{project_id}/compare")
    def compare_experiments(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        from tirkit.report import compare_json, render_compare
        from tirkit.runner import compare

        q = request.query_params
        try:
            a = ctx.store.load_experiment(q["baseline"])
            b = ctx.store.load_experiment(q["candidate"])
            comps = compare(a, b)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            examples = [asdict(e) for e in ctx.store.load_dataset(a.dataset, a.dataset_version)]
        except (KeyError, ValueError):
            examples = []
        items = [
            {
                "idx": i,
                "example": examples[i] if i < len(examples) else None,
                "baseline": {
                    "output": a.outputs[i],
                    "trace_id": a.trace_ids[i] if i < len(a.trace_ids) else None,
                    "scores": {k: v[i] for k, v in a.scores.items()},
                },
                "candidate": {
                    "output": b.outputs[i],
                    "trace_id": b.trace_ids[i] if i < len(b.trace_ids) else None,
                    "scores": {k: v[i] for k, v in b.scores.items()},
                },
            }
            for i in range(len(a.outputs))
        ]
        return _json(
            {
                "baseline": a.experiment_id,
                "candidate": b.experiment_id,
                "comparisons": compare_json(comps),
                "markdown": render_compare(comps, a, b),
                "items": items,
            }
        )

    # -- extension point: later milestones register more routes here --------------------------

    from tirkit.server import extensions  # noqa: F401  (registers later milestones' routes)

    for register in _EXTENSIONS:
        register(app, project_ctx, key_project, _json)

    # -- web UI ------------------------------------------------------------------------------------

    web_dir = settings.web_dir or _default_web_dir()
    if web_dir and (web_dir / "index.html").exists():
        index = web_dir / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Response:
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = (web_dir / path).resolve()
            if path and candidate.is_file() and web_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

    else:

        @app.get("/", include_in_schema=False)
        def no_ui() -> PlainTextResponse:
            return PlainTextResponse(
                "Tirkit server is running. The web UI is not built: run `make web`. API: /api/docs"
            )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


_EXTENSIONS: list[Callable[..., None]] = []


def extension(fn: Callable[..., None]) -> Callable[..., None]:
    """Register extra routes: ``fn(app, project_ctx, key_project, json_response)``."""
    _EXTENSIONS.append(fn)
    return fn


def _default_web_dir() -> Path | None:
    here = Path(__file__).resolve()
    for candidate in (here.parent / "web", here.parents[3] / "web" / "dist"):
        if (candidate / "index.html").exists():
            return candidate
    return None
