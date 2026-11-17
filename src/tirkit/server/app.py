"""FastAPI application (SPEC FR-22–FR-26).

``create_app`` wires per-app state (store, accounts, media, cache) and includes one router per
domain from :mod:`tirkit.server.routers`. Authentication and the caller context live in
:mod:`tirkit.server.deps`; request and response models in :mod:`tirkit.server.models`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from tirkit.accounts import Accounts
from tirkit.media import LocalMediaStore
from tirkit.server import openapi, prometheus
from tirkit.server.deps import (
    SESSION_COOKIE,
    Ctx,
    TTLCache,
    json_response,
    key_project,
    project_ctx,
)
from tirkit.server.routers import ROUTERS
from tirkit.server.settings import Settings
from tirkit.store import open_store

__all__ = ["SESSION_COOKIE", "Ctx", "create_app", "extension"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = open_store(settings.database_url)
    store.init_schema()
    accounts = Accounts(store)
    if settings.admin_email and settings.admin_password and accounts.count_users() == 0:
        admin = accounts.create_user(settings.admin_email, settings.admin_password, is_admin=True)
        accounts.create_project("Default", admin.user_id)

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
        description='Tirkit HTTP API. Errors are always `{"detail": "..."}`.',
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        separate_input_output_schemas=False,
    )
    app.state.store = store
    app.state.accounts = accounts
    app.state.settings = settings
    app.state.media = LocalMediaStore(settings.media_dir)
    app.state.cache = TTLCache()

    for router in ROUTERS:
        app.include_router(router, responses=openapi.ERROR_RESPONSES)
    prometheus.install(app, store, settings.metrics_enabled, settings.metrics_token)

    # Extension point for plugins: ``fn(app, project_ctx, key_project, json_response)``.
    for register in _EXTENSIONS:
        register(app, project_ctx, key_project, json_response)

    _mount_web(app, settings.web_dir or _default_web_dir())

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Validation errors use the same ``{"detail": "..."}`` shape and 400 as other errors."""
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return JSONResponse({"detail": "; ".join(parts) or "invalid request"}, status_code=400)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = openapi.build(app)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def _mount_web(app: FastAPI, web_dir: Path | None) -> None:
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
