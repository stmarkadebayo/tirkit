"""Audit log (B6) and API key rotation, expiry and last-used (B7)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("fastapi")

from tests.test_server import PASSWORD, basic, new_key, setup_admin  # noqa: E402
from tirkit import audit  # noqa: E402
from tirkit.accounts import Accounts  # noqa: E402


@pytest.fixture
def client(tmp_path: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from tests.test_server import make_settings
    from tirkit.server.app import create_app

    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


def _ingest(c: Any, pk: str, sk: str) -> int:
    body = json.dumps({"spans": [{"trace_id": "t", "span_id": "s", "name": "x"}]})
    h = {**basic(pk, sk), "content-type": "application/json"}
    return int(c.post("/api/v1/ingest", content=body, headers=h).status_code)


# -- B6 ---------------------------------------------------------------------------------------


def test_audit_log_is_append_only(store: Any) -> None:
    audit.record(store, "login.success", actor="a@example.com", ip="1.2.3.4")
    with pytest.raises(Exception, match="append-only"):
        store.execute("UPDATE audit_log SET actor = 'mallory'")
    with pytest.raises(Exception, match="append-only"):
        store.execute("DELETE FROM audit_log")
    assert audit.entries(store)[0]["actor"] == "a@example.com"
    with pytest.raises(ValueError):
        audit.record(store, "no.such.action")


def test_audit_events_recorded_through_the_api(client: Any) -> None:
    pid, _ = setup_admin(client)
    pk, _ = new_key(client, pid)
    client.post(f"/api/projects/{pid}/keys/{pk}/rotate", json={"overlap_s": 0})
    client.delete(f"/api/projects/{pid}/keys/{pk}")
    client.put(f"/api/projects/{pid}/settings", json={"redact_mode": "strict"})
    client.post(
        f"/api/projects/{pid}/members",
        json={"email": "m@example.com", "password": "another long password", "role": "viewer"},
    )
    uid = client.get(f"/api/projects/{pid}/members").json()
    uid = next(m["user_id"] for m in uid if m["email"] == "m@example.com")
    client.delete(f"/api/projects/{pid}/members/{uid}")
    client.get(f"/api/projects/{pid}/export/traces.jsonl")
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "nope nope nope"}
    )
    client.post("/api/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    client.headers["X-CSRF-Token"] = client.get("/api/auth/me").json()["csrf"]

    page = client.get(f"/api/projects/{pid}/audit").json()
    actions = [e["action"] for e in page["entries"]]
    for a in (
        "setup",
        "key.create",
        "key.rotate",
        "key.delete",
        "settings.update",
        "member.add",
        "member.remove",
        "export.traces",
    ):
        assert a in actions, (a, actions)
    rotate = next(e for e in page["entries"] if e["action"] == "key.rotate")
    assert rotate["actor"] == "admin@example.com" and rotate["ip"] and rotate["target"] == pk
    assert rotate["detail"]["new_key"].startswith("pk-tirkit-")
    # Sign-ins have no project; the server-wide log shows them to server admins.
    logins = client.get("/api/admin/audit", params={"action": "login."}).json()["entries"]
    assert [e["action"] for e in logins] == ["login.success", "login.failure"]
    assert logins[1]["actor"] == "admin@example.com"
    # Paging.
    first = client.get(f"/api/projects/{pid}/audit", params={"limit": 2}).json()
    assert len(first["entries"]) == 2 and first["next_before"] == first["entries"][-1]["id"]
    nxt = client.get(
        f"/api/projects/{pid}/audit", params={"limit": 2, "before": first["next_before"]}
    ).json()
    assert nxt["entries"][0]["id"] < first["next_before"]


def test_audit_api_requires_admin(client: Any) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    # API keys cannot read the audit log.
    r = client.get(f"/api/projects/{pid}/audit", headers=basic(pk, sk))
    assert r.status_code == 403
    client.post(
        f"/api/projects/{pid}/members",
        json={"email": "v@example.com", "password": "another long password", "role": "member"},
    )
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"email": "v@example.com", "password": "another long password"}
    ).is_success
    client.headers["X-CSRF-Token"] = client.get("/api/auth/me").json()["csrf"]
    assert client.get(f"/api/projects/{pid}/audit").status_code == 403
    assert client.get("/api/admin/audit").status_code == 403


def test_api_key_audit_actor(client: Any) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    client.cookies.clear()
    r = client.get(f"/api/projects/{pid}/export/traces.jsonl", headers=basic(pk, sk))
    assert r.status_code == 200
    entries = audit.entries(client.app.state.store, action="export.traces")
    assert entries[0]["actor"] == f"api:{pk}"


# -- B7 ---------------------------------------------------------------------------------------


def test_rotation_overlap_then_old_key_stops(client: Any, monkeypatch: Any) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    assert _ingest(client, pk, sk) == 200  # cached as valid
    r = client.post(f"/api/projects/{pid}/keys/{pk}/rotate", json={"overlap_s": 3600})
    assert r.status_code == 201
    new = r.json()
    assert new["previous_key"] == pk and new["public_key"] != pk
    assert new["previous_expires_at"] == pytest.approx(time.time() + 3600, abs=5)
    # Both work during the overlap.
    assert _ingest(client, pk, sk) == 200
    assert _ingest(client, new["public_key"], new["secret_key"]) == 200
    keys = {k["public_key"]: k for k in client.get(f"/api/projects/{pid}/keys").json()}
    assert keys[pk]["rotated_to"] == new["public_key"] and not keys[pk]["expired"]
    # After the overlap only the new key works (even though the old one was cached).
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3601)
    assert _ingest(client, pk, sk) == 401
    assert _ingest(client, new["public_key"], new["secret_key"]) == 200
    keys = {k["public_key"]: k for k in client.get(f"/api/projects/{pid}/keys").json()}
    assert keys[pk]["expired"] is True
    # A rotated key cannot be rotated again.
    assert client.post(f"/api/projects/{pid}/keys/{pk}/rotate", json={}).status_code == 400
    assert client.post(f"/api/projects/{pid}/keys/pk-nope/rotate", json={}).status_code == 404


def test_rotation_with_zero_overlap_revokes_now(client: Any) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    assert _ingest(client, pk, sk) == 200
    client.post(f"/api/projects/{pid}/keys/{pk}/rotate", json={"overlap_s": 0})
    assert _ingest(client, pk, sk) == 401


def test_key_expiry_and_last_used(client: Any, monkeypatch: Any) -> None:
    pid, _ = setup_admin(client)
    soon = time.time() + 600
    r = client.post(f"/api/projects/{pid}/keys", json={"name": "temp", "expires_at": soon})
    assert r.status_code == 201 and r.json()["expires_at"] == soon
    pk, sk = r.json()["public_key"], r.json()["secret_key"]
    before = next(
        k for k in client.get(f"/api/projects/{pid}/keys").json() if k["public_key"] == pk
    )
    assert before["last_used"] is None and before["expires_at"] == soon
    assert _ingest(client, pk, sk) == 200
    after = next(k for k in client.get(f"/api/projects/{pid}/keys").json() if k["public_key"] == pk)
    assert after["last_used"] is not None
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 601)
    assert _ingest(client, pk, sk) == 401
    # Expiry in the past is refused.
    r = client.post(f"/api/projects/{pid}/keys", json={"expires_at": real() - 1})
    assert r.status_code == 400


def test_accounts_rotation_keeps_earlier_expiry(store: Any) -> None:
    acc = Accounts(store)
    pid = acc.create_project("p")
    exp = time.time() + 60
    pk, _ = acc.create_api_key(pid, "k", expires_at=exp)
    _, _, old = acc.rotate_api_key(pid, pk, overlap_s=86400)
    assert old == pytest.approx(exp)


# -- Postgres: the same store-level behaviour (skipped without TIRKIT_TEST_POSTGRES_URL) -------


@pytest.fixture
def pg_store() -> Iterator[Any]:
    import os

    url = os.environ.get("TIRKIT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TIRKIT_TEST_POSTGRES_URL not set")
    from tirkit.store import open_store

    st = open_store(url)
    st.init_schema()
    yield st
    st.close()


@pytest.mark.postgres
def test_postgres_audit_keys_and_redaction_counter(pg_store: Any) -> None:
    audit.record(pg_store, "key.create", actor="a@example.com", detail={"k": 1})
    with pytest.raises(Exception, match="append-only"):
        pg_store.execute("UPDATE audit_log SET actor = 'mallory'")
    with pytest.raises(Exception, match="append-only"):
        pg_store.execute("DELETE FROM audit_log")
    assert audit.entries(pg_store, action="key.")[0]["detail"] == {"k": 1}

    acc = Accounts(pg_store)
    pid = acc.create_project("pg-security")
    pk, sk = acc.create_api_key(pid, "k")
    new_pk, new_sk, expiry = acc.rotate_api_key(pid, pk, overlap_s=60)
    assert acc.verify_api_key(pk, sk) == pid and acc.verify_api_key(new_pk, new_sk) == pid
    acc.rotate_api_key(pid, new_pk, overlap_s=0)
    assert acc.verify_api_key(new_pk, new_sk) is None
    assert {k["public_key"] for k in acc.list_api_keys(pid)} >= {pk, new_pk}

    view = pg_store.for_project(pid)
    view.add_redactions(3)
    view.add_redactions(4)
    assert view.redaction_count() == 7
