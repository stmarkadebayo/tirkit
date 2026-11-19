"""Server configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("TIRKIT_DATABASE_URL", "tirkit.db")
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

    # -- security (docs/site/security.md) ------------------------------------------------------
    # Reverse proxies in front of the server that append to X-Forwarded-For. 0 = use the socket
    # address and ignore forwarding headers (they can be forged by any client).
    trusted_proxies: int = field(default_factory=lambda: _int("TIRKIT_TRUSTED_PROXIES", 0))
    # Browser origins allowed to call the ingestion routes (comma-separated, or "*"). The UI and
    # the rest of the API are same-origin only.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip().rstrip("/")
            for o in os.environ.get("TIRKIT_CORS_ORIGINS", "").split(",")
            if o.strip()
        ]
    )
    # "database" shares limits between server processes; "memory" counts per process.
    rate_limit_backend: str = field(
        default_factory=lambda: os.environ.get("TIRKIT_RATE_LIMIT_BACKEND", "database")
    )
    # Spans per second per API key (token bucket); 0 disables. Burst defaults to 10 s of spans.
    ingest_spans_per_s: float = field(
        default_factory=lambda: float(os.environ.get("TIRKIT_INGEST_SPANS_PER_S", "5000"))
    )
    ingest_burst: float | None = field(
        default_factory=lambda: (
            float(os.environ["TIRKIT_INGEST_BURST"])
            if os.environ.get("TIRKIT_INGEST_BURST")
            else None
        )
    )
    # Login throttling: free attempts, then a delay doubling from 1 s up to the cap.
    login_free_per_account: int = 5
    login_free_per_ip: int = 20
    login_max_delay_s: float = 300.0
    login_reset_after_s: float = 3600.0
    # Logins per second across the whole server (token bucket, burst = 10 s worth).
    login_global_per_s: float = 10.0
