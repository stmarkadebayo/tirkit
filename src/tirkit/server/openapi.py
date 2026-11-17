"""OpenAPI document generation.

The schema is generated from the routes and :mod:`tirkit.server.models`, checked in as
``docs/api/openapi.json`` (``make api`` regenerates it, CI fails on a diff), and turned into the
web UI's TypeScript types. Run ``python -m tirkit.server.openapi`` to print it.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from tirkit.server import models as m

REF = "#/components/schemas/{model}"

# Models that describe bodies parsed by hand (see routers/ingest.py) plus the error shape.
_DOC_ONLY: list[type[BaseModel]] = [m.IngestRequest, m.Error]


def json_body(model: type[BaseModel]) -> dict[str, Any]:
    """``openapi_extra`` for a route that parses a JSON body itself."""
    if model not in _DOC_ONLY:
        _DOC_ONLY.append(model)
    schema = {"$ref": REF.format(model=model.__name__)}
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def binary_body(*types: str, description: str = "", text: bool = False) -> dict[str, Any]:
    """``openapi_extra`` for a raw (non-JSON-object) request body."""
    schema = {"type": "string"} if text else {"type": "string", "format": "binary"}
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {t: {"schema": schema} for t in types},
        }
    }


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "4XX": {"model": m.Error, "description": 'Client error: {"detail": "..."}'},
}


def build(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document for ``app``: deterministic, errors as ``Error``, no 422s (validation
    errors are returned as 400 with the same ``{"detail": ...}`` shape as every other error)."""
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        separate_input_output_schemas=False,
    )
    comps: dict[str, Any] = schema.setdefault("components", {}).setdefault("schemas", {})
    _, defs = models_json_schema([(x, "validation") for x in _DOC_ONLY], ref_template=REF)
    for name, sub in defs.get("$defs", {}).items():
        comps.setdefault(name, sub)
    comps["Json"] = m.JSON_SCHEMA
    schema = _resolve_json_markers(schema)
    comps = schema["components"]["schemas"]
    for name in ("HTTPValidationError", "ValidationError"):
        comps.pop(name, None)
    for path in schema.get("paths", {}).values():
        for op in path.values():
            op.get("responses", {}).pop("422", None)
    schema["components"]["schemas"] = dict(sorted(comps.items()))
    return schema


def _resolve_json_markers(node: Any) -> Any:
    """Replace each ``Json`` marker with a ``$ref`` to the recursive ``Json`` schema."""
    if isinstance(node, dict):
        if node.get(m.JSON_MARKER):
            keep = {k: node[k] for k in ("title", "description", "default") if k in node}
            return {"$ref": REF.format(model="Json"), **keep}
        return {k: _resolve_json_markers(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_json_markers(v) for v in node]
    return node


def main() -> None:
    import tempfile
    from pathlib import Path

    from tirkit.server.app import create_app
    from tirkit.server.settings import Settings

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings()
        settings.database_url = ":memory:"
        settings.media_dir = Path(tmp) / "media"
        settings.web_dir = None
        settings.embedded_worker = False
        settings.admin_email = settings.admin_password = None
        app = create_app(settings)
        sys.stdout.write(json.dumps(app.openapi(), indent=2) + "\n")


if __name__ == "__main__":
    main()
