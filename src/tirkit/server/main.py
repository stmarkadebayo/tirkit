"""ASGI entry point: ``uvicorn tirkit.server.main:app``."""

from tirkit.server import extensions  # noqa: F401  (registers routes of later milestones)
from tirkit.server.app import create_app

app = create_app()
