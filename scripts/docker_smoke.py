"""Smoke test of a running Tirkit server, as a new user meets it: the UI is served, the first
admin can be created, an API key works, a trace sent through the Python SDK shows up, and the
metrics endpoint answers. CI runs it against the Docker image (`.github/workflows/docker.yml`).

    python scripts/docker_smoke.py --url http://localhost:8000

Exits 0 when every step passes. Uses a throwaway admin account with a random password.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from collections.abc import Callable

import httpx


def step(name: str) -> None:
    print(f"- {name}", flush=True)


def wait_for(check: Callable[[], bool], what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last = exc
        time.sleep(2)
    raise SystemExit(f"FAILED: {what} (waited {timeout:.0f}s; last error: {last!r})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--startup-timeout", type=float, default=180.0)
    args = ap.parse_args()
    url = args.url.rstrip("/")

    with httpx.Client(base_url=url, timeout=20) as c:
        step("server answers /api/health")
        wait_for(lambda: c.get("/api/health").is_success, "server health", args.startup_timeout)

        step("web UI is served at /")
        page = c.get("/")
        if not page.is_success or 'id="root"' not in page.text:
            raise SystemExit(f"FAILED: / did not return the web UI (HTTP {page.status_code})")

        step("first-run setup creates the admin and a project")
        if not c.get("/api/setup").json()["needs_setup"]:
            raise SystemExit("FAILED: the server is not fresh (setup already done)")
        email = f"smoke-{secrets.token_hex(4)}@example.test"
        password = secrets.token_urlsafe(16)
        r = c.post(
            "/api/setup",
            json={"email": email, "password": password, "name": "Smoke", "project_name": "Smoke"},
        )
        r.raise_for_status()
        pid = r.json()["project_id"]

        step("sign in and create an API key")
        c.post("/api/auth/login", json={"email": email, "password": password}).raise_for_status()
        # State-changing requests made with a session must echo the CSRF cookie in a header.
        c.headers["X-CSRF-Token"] = c.cookies.get("tirkit_csrf") or ""
        r = c.post(f"/api/projects/{pid}/keys", json={"name": "smoke"})
        r.raise_for_status()
        key = r.json()

        step("a trace sent with the Python SDK arrives")
        import tirkit

        tirkit.init(host=url, public_key=key["public_key"], secret_key=key["secret_key"])
        with tirkit.span("smoke-step", kind="agent", input="hello") as s:
            s.output = "world"
        tirkit.flush()
        wait_for(
            lambda: (c.get(f"/api/projects/{pid}/traces").json()["total"] or 0) >= 1,
            "the trace to appear in the trace list",
            30,
        )

        step("the same trace is returned by the API with its input and output")
        item = c.get(f"/api/projects/{pid}/traces").json()["items"][0]
        trace = c.get(f"/api/projects/{pid}/traces/{item['trace_id']}?full=1").json()
        if "world" not in str(trace):
            raise SystemExit("FAILED: the stored trace does not contain the span's output")

        step("/metrics answers (shared across server processes)")
        m = c.get("/metrics")
        if m.status_code != 200 or "tirkit_" not in m.text:
            raise SystemExit(f"FAILED: /metrics gave HTTP {m.status_code} without tirkit_ series")

    print("OK: every step passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
