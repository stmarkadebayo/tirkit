"""ASGI entry point: ``uvicorn tirkit.server.main:app``."""

from tirkit.server.app import create_app

app = create_app()
