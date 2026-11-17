"""Seed a local development server with an admin account and demo traffic.

    tirkit serve --port 8765            # in one terminal
    python examples/seed_dev.py --url http://127.0.0.1:8765

Development only: the account below is a fixed test login for a local instance. Never use it on
a server reachable by anyone else.

Sends: RAG agent traces (sessions, users, a tool call, PII), a vision call with an image, a
speech-to-text call and a text-to-speech call with audio, a failing call, and end-user feedback.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import struct
import subprocess
import sys
import wave
import zlib
from pathlib import Path

import httpx

DEV_EMAIL = "admin@tirkit.test"
DEV_PASSWORD = "tirkit-dev-only-password"


def wav_tone(seconds: float = 1.2, rate: int = 16000, freq: float = 440.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return buf.getvalue()


def png_square(size: int = 64) -> bytes:
    """A small valid PNG (purple square with a lighter centre)."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inner = size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
            row += bytes((190, 170, 255) if inner else (106, 76, 245))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--questions", type=int, default=60)
    args = ap.parse_args()

    c = httpx.Client(base_url=args.url, timeout=30)
    if c.get("/api/setup").json()["needs_setup"]:
        c.post(
            "/api/setup",
            json={
                "email": DEV_EMAIL,
                "password": DEV_PASSWORD,
                "name": "Dev Admin",
                "project_name": "Acme support bot",
            },
        ).raise_for_status()
    else:
        c.post(
            "/api/auth/login", json={"email": DEV_EMAIL, "password": DEV_PASSWORD}
        ).raise_for_status()
    me = c.get("/api/auth/me").json()
    c.headers["X-CSRF-Token"] = me["csrf"]
    pid = me["projects"][0]["project_id"]
    keys = c.post(f"/api/projects/{pid}/keys", json={"name": "seed"}).json()
    env = {
        **os.environ,
        "TIRKIT_HOST": args.url,
        "TIRKIT_PUBLIC_KEY": keys["public_key"],
        "TIRKIT_SECRET_KEY": keys["secret_key"],
    }
    demo = Path(__file__).with_name("rag_demo.py")
    subprocess.run(
        [sys.executable, str(demo), "--questions", str(args.questions)], env=env, check=True
    )

    os.environ.update(env)
    import tirkit

    tirkit.init()
    with tirkit.trace_context(session_id="vision-1", user_id="user-7", release="demo-v1"):
        with tirkit.span(
            "describe-photo", kind="agent", input={"question": "What is in this photo?"}
        ) as root:
            with tirkit.span(
                "gpt-4o vision",
                kind="llm",
                model="gpt-4o",
                input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "What is in this photo?"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": tirkit.Attachment(data=png_square(), mime="image/png")
                                    },
                                },
                            ],
                        }
                    ]
                },
            ) as s:
                s.output = {
                    "role": "assistant",
                    "content": "A purple square with a lighter square in the middle.",
                }
                s.set_usage(input_tokens=310, output_tokens=14)
            root.output = "A purple square with a lighter square in the middle."
    audio = wav_tone()
    with tirkit.trace_context(session_id="voice-1", user_id="user-3", release="demo-v1"):
        with tirkit.span(
            "voice-turn",
            kind="agent",
            input={"audio": tirkit.Attachment(data=audio, mime="audio/wav")},
        ) as root:
            with tirkit.span(
                "transcribe",
                kind="speech_to_text",
                model="whisper-1",
                input={"audio": tirkit.Attachment(data=audio, mime="audio/wav")},
            ) as s:
                s.output = {"text": "Where is my order?"}
                s.set_usage(input_audio_seconds=1.2)
                s.attributes["real_time_factor"] = 0.21
            with tirkit.span(
                "speak",
                kind="text_to_speech",
                model="tts-1",
                input={"text": "Your order shipped yesterday."},
            ) as s:
                s.output = {
                    "audio": tirkit.Attachment(data=wav_tone(1.0, freq=330), mime="audio/wav")
                }
                s.set_usage(input_characters=29, output_audio_seconds=1.0)
            root.output = "Your order shipped yesterday."
    try:
        with tirkit.span(
            "flaky-call", kind="llm", model="gpt-4o-mini", input="Summarise the ticket"
        ) as s:
            raise TimeoutError("upstream timed out after 30s")
    except TimeoutError:
        pass
    tirkit.feedback(root.trace_id, 1, comment="helpful voice answer")
    tirkit.flush()
    print(f"seeded project {pid} at {args.url} (login: see DEV_EMAIL / DEV_PASSWORD in this file)")


if __name__ == "__main__":
    main()
