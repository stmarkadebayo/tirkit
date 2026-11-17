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
    max_ingest_bytes: int = 20 * 1024 * 1024
    max_media_bytes: int = 50 * 1024 * 1024
    # Optional first admin created at startup (otherwise use the setup page).
    admin_email: str | None = field(default_factory=lambda: os.environ.get("TIRKIT_ADMIN_EMAIL"))
    admin_password: str | None = field(
        default_factory=lambda: os.environ.get("TIRKIT_ADMIN_PASSWORD")
    )
