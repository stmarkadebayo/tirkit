"""Install the built wheel into a clean virtualenv and use it the way a new user does, with no
Docker and nothing from the source tree on the path.

    python scripts/install_smoke.py dist/tirkit-0.1.0-py3-none-any.whl

1. ``pip install <wheel>`` alone is enough to trace: the core install must not pull in the
   server stack, and tracing with no server writes ``tirkit.db`` in the working directory.
2. ``pip install "<wheel>[server]"`` then ``tirkit serve`` with no arguments opens that same
   file: the first admin's project already holds the trace recorded in step 1 (the web UI is in
   the wheel), and a trace sent over HTTP arrives (``scripts/docker_smoke.py``).

Exits 0 when every step passes. Runs on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent

APP = """
import sys
import tirkit

tirkit.init()

@tirkit.trace(kind="agent")
def answer(question: str) -> str:
    return "Nairobi"

answer("What is the capital of Kenya?")
assert tirkit.flush(), "flush timed out"
heavy = [m for m in ("fastapi", "uvicorn", "psycopg", "argon2") if m in sys.modules]
assert not heavy, f"the core install imported server packages: {heavy}"
print("traced locally")
"""


def step(msg: str) -> None:
    print(f"- {msg}", flush=True)


def run(cmd: list[str], **kw: object) -> None:
    subprocess.run(cmd, check=True, **kw)  # type: ignore[call-overload]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wheel", type=Path)
    args = ap.parse_args()
    wheel = args.wheel.resolve()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        env_dir = root / "venv"
        work = root / "work"
        work.mkdir()
        venv.EnvBuilder(with_pip=True).create(env_dir)
        bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        py = str(bindir / ("python.exe" if os.name == "nt" else "python"))
        tirkit_cli = str(bindir / ("tirkit.exe" if os.name == "nt" else "tirkit"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("TIRKIT_")}
        env["PYTHONUTF8"] = "1"

        step("pip install the wheel (core only)")
        run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", str(wheel)])

        step("tirkit --help runs")
        run([tirkit_cli, "--help"], stdout=subprocess.DEVNULL, env=env)

        step("trace with no server: tirkit.db is written, no server packages imported")
        (work / "app.py").write_text(APP)
        run([py, "app.py"], cwd=work, env=env)
        if not (work / "tirkit.db").exists():
            print("FAILED: tracing without a server did not create tirkit.db")
            return 1

        step('pip install "tirkit[server]"')
        run(
            [py, "-m", "pip", "install", "-q", "--disable-pip-version-check", f"{wheel}[server]"]
        )

        step("tirkit serve with no arguments opens that same tirkit.db")
        port = free_port()
        log = (work / "serve.log").open("w")
        proc = subprocess.Popen(
            [tirkit_cli, "serve", "--port", str(port)],
            cwd=work,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            smoke = [
                py,
                str(HERE / "docker_smoke.py"),
                "--url",
                f"http://127.0.0.1:{port}",
                "--startup-timeout",
                "60",
                "--expect-traces",
                "1",
            ]
            result = subprocess.run(smoke, cwd=work, env=env)
            if result.returncode != 0:
                log.flush()
                print((work / "serve.log").read_text(errors="replace")[-3000:])
                return 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            time.sleep(0.5)
    print("OK: install and first use work from the wheel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
