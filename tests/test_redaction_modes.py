"""PII redaction modes (B10): off / secrets / strict, counting, phone false positives, the
migration rule for existing projects, server ingestion and SDK-side redaction."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tirkit.redact import MODES, Redactor, mode_from_settings, redact


@pytest.fixture
def client(tmp_path: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from tests.test_server import make_settings
    from tirkit.server.app import create_app

    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


SECRET_TEXT = "key sk-proj-abcdefghijklmnop1234 card 4111 1111 1111 1111 password=hunter2hunter2"
PII_TEXT = "mail ana@example.com, call +1 415 555 0134 from 10.1.2.3"


def test_modes() -> None:
    assert MODES == ("off", "secrets", "strict")
    assert Redactor("off").text(SECRET_TEXT + PII_TEXT) == SECRET_TEXT + PII_TEXT
    sec = Redactor("secrets").text(SECRET_TEXT + " " + PII_TEXT)
    assert "sk-proj" not in sec and "4111" not in sec and "hunter2" not in sec
    assert "ana@example.com" in sec and "415 555 0134" in sec and "10.1.2.3" in sec
    strict = Redactor("strict").text(PII_TEXT)
    assert strict == "mail [EMAIL], call [PHONE] from [IP]"
    with pytest.raises(ValueError):
        Redactor("loud")


def test_secrets_mode_catches_tokens_jwts_ibans() -> None:
    text = (
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9 "
        "aws AKIAIOSFODNN7EXAMPLE gh ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "iban DE89 3704 0044 0532 0130 00"
    )
    out = redact(text, mode="secrets")
    assert out.count("[API_KEY]") == 3 and "[IBAN]" in out


def test_counts() -> None:
    r = Redactor("strict")
    r.obj({"a": PII_TEXT, "b": ["x@y.io", 3], "c": "nothing here"})
    assert r.count == 4
    r = Redactor("secrets")
    r.text("card 4111 1111 1111 1112")  # fails Luhn: not a card, not counted
    assert r.count == 0


def test_extra_patterns_in_active_modes_only() -> None:
    extra = [(r"CUST-\d+", "[CUSTOMER]")]
    assert Redactor("secrets", extra).text("CUST-123") == "[CUSTOMER]"
    assert Redactor("off", extra).text("CUST-123") == "CUST-123"


@pytest.mark.parametrize(
    "text",
    [
        "epoch 1700000000",
        "ms 1700000000123",
        "float 1700000000.123",
        "order 123456789012",
        "ref ORD-2024-000123",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "date 2024-01-15 10:30:00",
        "date 15/01/2024",
        "date 2024.01.15",
        "pi 3.14159265358",
        "lat 37.7749, -122.4194",
        "trace 4bf92f3577b34da6a3ce929d0e0e4736",
        "span 00f067aa0ba902b7",
        "build 20240115123045",
        "price 1,299,999.99",
        "sizes 1024 2048 4096",
        "range 100 200 300",
    ],
)
def test_phone_false_positives(text: str) -> None:
    """Long numeric IDs, timestamps, dates and numbers survive strict mode."""
    assert redact(text, mode="strict") == text


@pytest.mark.parametrize(
    "text",
    [
        "+1 415 555 0134",
        "+44 20 7946 0958",
        "(212) 555-0198",
        "415-555-0134",
        "415.555.0134",
        "020 7946 0958",
        "+2348031234567",
    ],
)
def test_phone_true_positives(text: str) -> None:
    assert redact(f"call {text} now", mode="strict") == "call [PHONE] now"


@pytest.mark.parametrize(
    "settings,mode",
    [
        (None, "off"),
        ({}, "off"),
        ({"redact": False}, "off"),
        ({"redact": True}, "strict"),  # projects that had redaction on keep the full set
        ({"redact_mode": "secrets"}, "secrets"),
        ({"redact_mode": "off", "redact": True}, "off"),
        ({"redact_mode": "bogus", "redact": True}, "strict"),
    ],
)
def test_existing_projects_keep_their_behaviour(settings: Any, mode: str) -> None:
    assert mode_from_settings(settings) == mode


# -- server ------------------------------------------------------------------------------------


def _ingest(c: Any, pk: str, sk: str, text: str, attrs: dict[str, Any] | None = None) -> None:
    from tests.test_server import basic

    span = {"trace_id": "t-" + str(time.time()), "span_id": "s", "name": "x", "input": text}
    if attrs:
        span["attributes"] = attrs
    r = c.post(
        "/api/v1/ingest",
        content=json.dumps({"spans": [span]}),
        headers={**basic(pk, sk), "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text


def _last_input(c: Any, pid: str) -> Any:
    spans = c.get(f"/api/projects/{pid}/spans").json()["items"]
    return max(spans, key=lambda s: s["trace_id"])["input"]


@pytest.mark.server
def test_new_project_defaults_to_secrets_and_counts(client: Any) -> None:
    from tests.test_server import new_key, setup_admin

    pid, _ = setup_admin(client)
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["settings"]["redact_mode"] == "secrets" and proj["redactions"] == 0
    pk, sk = new_key(client, pid)
    _ingest(client, pk, sk, SECRET_TEXT + " " + PII_TEXT)
    stored = _last_input(client, pid)
    assert "sk-proj" not in stored and "ana@example.com" in stored
    assert client.get(f"/api/projects/{pid}").json()["redactions"] == 3
    # Strict.
    r = client.put(f"/api/projects/{pid}/settings", json={"redact_mode": "strict"})
    assert r.json()["redact_mode"] == "strict"
    _ingest(client, pk, sk, PII_TEXT)
    assert _last_input(client, pid) == "mail [EMAIL], call [PHONE] from [IP]"
    # SDK-side counts are added (the SDK redacted before sending).
    _ingest(client, pk, sk, "clean", {"tirkit.redactions": 5})
    assert client.get(f"/api/projects/{pid}").json()["redactions"] == 3 + 3 + 5
    # The old on/off switch maps onto modes.
    assert (
        client.put(f"/api/projects/{pid}/settings", json={"redact": False}).json()["redact_mode"]
        == "off"
    )
    assert (
        client.put(f"/api/projects/{pid}/settings", json={"redact_mode": "loud"}).status_code == 400
    )
    cleared = client.put(f"/api/projects/{pid}/settings", json={"redact_mode": None})
    assert cleared.json()["redact_mode"] == "secrets"


@pytest.mark.server
def test_project_created_before_modes_keeps_old_behaviour(client: Any) -> None:
    """A project row with the old settings shape (no redact_mode) is not changed by the upgrade."""
    from tests.test_server import new_key, setup_admin

    pid, _ = setup_admin(client)
    accounts = client.app.state.accounts
    accounts.update_settings(pid, {"retention_days": 30})  # old project, redaction never enabled
    client.app.state.cache.drop("settings", pid)
    pk, sk = new_key(client, pid)
    _ingest(client, pk, sk, SECRET_TEXT)
    assert _last_input(client, pid) == SECRET_TEXT
    accounts.update_settings(pid, {"redact": True})  # old project with redaction enabled
    client.app.state.cache.drop("settings", pid)
    _ingest(client, pk, sk, PII_TEXT)
    assert _last_input(client, pid) == "mail [EMAIL], call [PHONE] from [IP]"


# -- SDK ---------------------------------------------------------------------------------------


def test_sdk_redacts_in_exporter_thread(tmp_path: Any) -> None:
    import tirkit
    from tirkit.exporter import BatchExporter
    from tirkit.tracing import Span

    seen_threads: list[str] = []

    class Target:
        spans: list[Span] = []

        def save_spans(self, spans: list[Span]) -> None:
            seen_threads.append(threading.current_thread().name)
            self.spans.extend(spans)

    target = Target()
    exp = BatchExporter(target, redact="strict")
    span = Span(name="x", input={"q": PII_TEXT}, output=SECRET_TEXT)
    exp.save_span(span)
    # Nothing was redacted on the caller's thread.
    assert span.input == {"q": PII_TEXT}
    exp.flush()
    exp.shutdown()
    out = target.spans[0]
    assert out.input == {"q": "mail [EMAIL], call [PHONE] from [IP]"}
    assert "sk-proj" not in out.output
    assert out.attributes["tirkit.redactions"] == 6
    assert seen_threads == ["tirkit-exporter"]
    assert exp.stats()["redacted"] == 6
    assert tirkit  # imported for the side effect of registering the package


def test_sdk_init_redact_option(tmp_path: Any, monkeypatch: Any) -> None:
    import tirkit
    from tirkit.tracing import span

    monkeypatch.delenv("TIRKIT_REDACT", raising=False)
    client = tirkit.init(path=tmp_path / "t.db", redact="secrets")
    with span("x", input="token=abcdefgh12345678") as s:
        tid = s.trace_id
    client.flush()
    stored = client.store.get_spans(tid)
    assert "abcdefgh12345678" not in json.dumps(stored[0].input)
    client.shutdown()
    with pytest.raises(ValueError):
        tirkit.init(path=tmp_path / "t2.db", redact="loud")
    monkeypatch.setenv("TIRKIT_REDACT", "strict")
    client = tirkit.init(path=tmp_path / "t3.db")
    assert client.exporter.redact == "strict"
    client.shutdown()


def test_api_key_trigger_covers_every_pattern() -> None:
    """The fast pre-check must match wherever an API_KEYS pattern can, or secrets would leak."""
    from tirkit.redact import API_KEY_TRIGGER, API_KEYS

    samples = [
        "sk-proj-abcdefghijklmnop1234",
        "sk-ant-api03-abcdefghijklmnop1234",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_" + "a" * 45,
        "xoxb-1234567890-abcdefgh",
        "AIza" + "a" * 35,
        "sk_live_" + "a" * 20,
        "pk_test_" + "a" * 20,
        "rk_live_" + "a" * 20,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9",
        "API_KEY: abcdefgh1234",
        "apikey=abcdefgh1234",
        "Secret = 'abcdefgh1234'",
        "TOKEN: abcdefgh1234",
        "password=hunter2hunter2",
        "passwd: abcdefgh1234",
        "pwd=abcdefgh1234",
    ]
    for text in samples:
        assert any(p.search(text) for p in API_KEYS), text  # a real positive for some pattern
        assert API_KEY_TRIGGER.search(text), text
    assert len(API_KEYS) == 9  # add a sample (and a trigger marker) when this grows
