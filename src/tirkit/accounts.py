"""Users, projects, memberships, API keys and sessions (SPEC FR-23, NFR-7).

Passwords and API-key secrets are hashed with argon2. Verified API keys are cached in memory for a
minute so ingestion does not pay an argon2 hash per request.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from tirkit.store import SQLStore

ROLES = ("admin", "member", "viewer")
SESSION_TTL_S = 14 * 24 * 3600
_KEY_CACHE_TTL_S = 60.0


def _hasher() -> Any:
    from argon2 import PasswordHasher

    return PasswordHasher()


def hash_secret(secret: str) -> str:
    return str(_hasher().hash(secret))


def verify_secret(hashed: str, secret: str) -> bool:
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    try:
        return bool(_hasher().verify(hashed, secret))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class User:
    user_id: str
    email: str
    name: str | None
    is_admin: bool


class Accounts:
    """Account operations on a store's database (tables are global, not project-scoped)."""

    def __init__(self, store: SQLStore) -> None:
        self.store = store
        self._key_cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # -- users ---------------------------------------------------------------------------------

    def count_users(self) -> int:
        return int(self.store.execute("SELECT COUNT(*) FROM users")[0][0])

    def create_user(
        self, email: str, password: str, *, name: str | None = None, is_admin: bool = False
    ) -> User:
        email = email.strip().lower()
        if "@" not in email:
            raise ValueError("invalid email")
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        user = User(uuid.uuid4().hex, email, name, is_admin)
        self.store.execute(
            "INSERT INTO users (user_id, email, name, password_hash, is_admin, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [user.user_id, email, name, hash_secret(password), int(is_admin), time.time()],
        )
        return user

    def get_user(self, user_id: str) -> User | None:
        rows = self.store.execute(
            "SELECT user_id, email, name, is_admin FROM users WHERE user_id = ?", [user_id]
        )
        return User(rows[0][0], rows[0][1], rows[0][2], bool(rows[0][3])) if rows else None

    def find_user(self, email: str) -> User | None:
        rows = self.store.execute(
            "SELECT user_id FROM users WHERE email = ?", [email.strip().lower()]
        )
        return self.get_user(rows[0][0]) if rows else None

    def authenticate(self, email: str, password: str) -> User | None:
        rows = self.store.execute(
            "SELECT user_id, password_hash FROM users WHERE email = ?", [email.strip().lower()]
        )
        if not rows:
            verify_secret(hash_secret("timing-equaliser"), password)
            return None
        return self.get_user(rows[0][0]) if verify_secret(rows[0][1], password) else None

    # -- sessions ------------------------------------------------------------------------------

    def create_session(self, user_id: str) -> tuple[str, str]:
        """Returns (session token, CSRF token). Only a hash of the session token is stored."""
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = time.time()
        self.store.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf, created, expires) "
            "VALUES (?, ?, ?, ?, ?)",
            [_sha(token), user_id, csrf, now, now + SESSION_TTL_S],
        )
        return token, csrf

    def session_user(self, token: str) -> tuple[User, str] | None:
        rows = self.store.execute(
            "SELECT user_id, csrf, expires FROM sessions WHERE token_hash = ?", [_sha(token)]
        )
        if not rows or rows[0][2] < time.time():
            return None
        user = self.get_user(rows[0][0])
        return (user, rows[0][1]) if user else None

    def delete_session(self, token: str) -> None:
        self.store.execute("DELETE FROM sessions WHERE token_hash = ?", [_sha(token)])

    # -- projects and members ------------------------------------------------------------------

    def create_project(self, name: str, owner_id: str | None = None) -> str:
        pid = "p_" + uuid.uuid4().hex[:12]
        self.store.execute(
            "INSERT INTO projects (project_id, name, settings, created) VALUES (?, ?, ?, ?)",
            [pid, name, None, time.time()],
        )
        if owner_id:
            self.set_role(owner_id, pid, "admin")
        return pid

    def project(self, project_id: str) -> dict[str, Any] | None:
        rows = self.store.execute(
            "SELECT project_id, name, settings, created FROM projects WHERE project_id = ?",
            [project_id],
        )
        if not rows:
            return None
        settings = self.store.dialect.load_json(rows[0][2]) or {}
        return {
            "project_id": rows[0][0],
            "name": rows[0][1],
            "settings": settings,
            "created": rows[0][3],
        }

    def update_settings(self, project_id: str, settings: dict[str, Any]) -> None:
        self.store.execute(
            "UPDATE projects SET settings = ? WHERE project_id = ?",
            [self.store._json(settings), project_id],
        )

    def set_role(self, user_id: str, project_id: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        self.store.execute(
            "INSERT INTO memberships (user_id, project_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, project_id) DO UPDATE SET role = excluded.role",
            [user_id, project_id, role],
        )

    def remove_member(self, user_id: str, project_id: str) -> None:
        self.store.execute(
            "DELETE FROM memberships WHERE user_id = ? AND project_id = ?", [user_id, project_id]
        )

    def role(self, user: User, project_id: str) -> str | None:
        rows = self.store.execute(
            "SELECT role FROM memberships WHERE user_id = ? AND project_id = ?",
            [user.user_id, project_id],
        )
        if rows:
            return str(rows[0][0])
        if user.is_admin and self.project(project_id):
            return "admin"
        return None

    def projects_for(self, user: User) -> list[dict[str, Any]]:
        if user.is_admin:
            rows = self.store.execute(
                "SELECT p.project_id, p.name, COALESCE(m.role, 'admin') FROM projects p "
                "LEFT JOIN memberships m ON m.project_id = p.project_id AND m.user_id = ? "
                "ORDER BY p.created",
                [user.user_id],
            )
        else:
            rows = self.store.execute(
                "SELECT p.project_id, p.name, m.role FROM projects p JOIN memberships m "
                "ON m.project_id = p.project_id WHERE m.user_id = ? ORDER BY p.created",
                [user.user_id],
            )
        return [{"project_id": r[0], "name": r[1], "role": r[2]} for r in rows]

    def members(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT u.user_id, u.email, u.name, m.role FROM memberships m JOIN users u "
            "ON u.user_id = m.user_id WHERE m.project_id = ? ORDER BY u.email",
            [project_id],
        )
        return [{"user_id": r[0], "email": r[1], "name": r[2], "role": r[3]} for r in rows]

    # -- API keys ------------------------------------------------------------------------------

    def create_api_key(self, project_id: str, name: str | None = None) -> tuple[str, str]:
        """Returns (public key, secret key). The secret is shown once and stored hashed."""
        public = "pk-tirkit-" + secrets.token_hex(12)
        secret = "sk-tirkit-" + secrets.token_urlsafe(32)
        self.store.execute(
            "INSERT INTO api_keys (public_key, secret_hash, project_id, name, created) "
            "VALUES (?, ?, ?, ?, ?)",
            [public, hash_secret(secret), project_id, name, time.time()],
        )
        return public, secret

    def list_api_keys(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT public_key, name, created, last_used FROM api_keys WHERE project_id = ? "
            "ORDER BY created",
            [project_id],
        )
        return [
            {"public_key": r[0], "name": r[1], "created": r[2], "last_used": r[3]} for r in rows
        ]

    def delete_api_key(self, project_id: str, public_key: str) -> None:
        self.store.execute(
            "DELETE FROM api_keys WHERE project_id = ? AND public_key = ?", [project_id, public_key]
        )
        with self._lock:
            self._key_cache = {
                k: v for k, v in self._key_cache.items() if not k.startswith(public_key + ":")
            }

    def verify_api_key(self, public_key: str, secret: str) -> str | None:
        """Project id for a valid key pair, else None."""
        cache_key = f"{public_key}:{_sha(secret)}"
        now = time.time()
        with self._lock:
            hit = self._key_cache.get(cache_key)
        if hit and now - hit[1] < _KEY_CACHE_TTL_S:
            return hit[0]
        rows = self.store.execute(
            "SELECT secret_hash, project_id FROM api_keys WHERE public_key = ?", [public_key]
        )
        if not rows or not verify_secret(rows[0][0], secret):
            return None
        project_id = str(rows[0][1])
        self.store.execute(
            "UPDATE api_keys SET last_used = ? WHERE public_key = ?", [now, public_key]
        )
        with self._lock:
            self._key_cache[cache_key] = (project_id, now)
        return project_id
