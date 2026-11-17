"""Server configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("TIRKIT_DATABASE_URL", "tirkit-server.db")
    )
    media_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("TIRKIT_MEDIA_DIR", "tirkit-media"))
    )
    web_dir: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["TIRKIT_WEB_DIR"]) if os.environ.get("TIRKIT_WEB_DIR") else None
        )
    )
    # Cookies are marked Secure unless explicitly running on plain HTTP (local development).
    secure_cookies: bool = field(
        default_factory=lambda: os.environ.get("TIRKIT_INSECURE_COOKIES", "") != "1"
    )
    # Run the job worker inside the server process (single-process installs). Compose runs a
    # separate `tirkit worker` service and sets this to 0.
    embedded_worker: bool = field(
        default_factory=lambda: os.environ.get("TIRKIT_EMBEDDED_WORKER", "1") != "0"
    )
    # Optional OTLP/gRPC receiver (off unless a port is set; 0 picks a free port).
    otlp_grpc_port: int | None = field(
        default_factory=lambda: (
            int(os.environ["TIRKIT_OTLP_GRPC_PORT"])
            if os.environ.get("TIRKIT_OTLP_GRPC_PORT")
            else None
        )
    )
    otlp_grpc_host: str = field(
        default_factory=lambda: os.environ.get("TIRKIT_OTLP_GRPC_HOST", "127.0.0.1")
    )
    max_ingest_bytes: int = 20 * 1024 * 1024
    max_media_bytes: int = 50 * 1024 * 1024
    # Optional first admin created at startup (otherwise use the setup page).
    admin_email: str | None = field(default_factory=lambda: os.environ.get("TIRKIT_ADMIN_EMAIL"))
    admin_password: str | None = field(
        default_factory=lambda: os.environ.get("TIRKIT_ADMIN_PASSWORD")
    )
    # Prometheus metrics at /metrics: TIRKIT_METRICS=0 turns them off; TIRKIT_METRICS_TOKEN, if set,
    # must be sent as "Authorization: Bearer <token>".
    metrics_enabled: bool = field(
        default_factory=lambda: os.environ.get("TIRKIT_METRICS", "1") != "0"
    )
    metrics_token: str | None = field(
        default_factory=lambda: os.environ.get("TIRKIT_METRICS_TOKEN") or None
    )
