"""HTTP routes, one module per domain. ``tirkit.server.app.create_app`` includes them in order."""

from __future__ import annotations

from fastapi import APIRouter

from tirkit.server.routers import (
    admin,
    annotation,
    datasets,
    experiments,
    ingest,
    online,
    projects,
    prompts,
    system,
    traces,
)

ROUTERS: list[APIRouter] = [
    system.router,
    projects.router,
    ingest.router,
    traces.router,
    datasets.router,
    experiments.router,
    prompts.router,
    annotation.router,
    online.router,
    admin.router,
]
