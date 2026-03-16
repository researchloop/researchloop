"""Simple password authentication for the dashboard."""

from __future__ import annotations

import secrets

import bcrypt
from itsdangerous import URLSafeTimedSerializer

SESSION_COOKIE = "researchloop_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


def check_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash."""
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


class SessionManager:
    """Manage signed session cookies."""

    def __init__(self, secret_key: str | None = None) -> None:
        self.secret_key = secret_key or secrets.token_hex(32)
        self._serializer = URLSafeTimedSerializer(self.secret_key)

    def create_token(self) -> str:
        return self._serializer.dumps({"authenticated": True})

    def verify_token(self, token: str) -> bool:
        try:
            data = self._serializer.loads(token, max_age=SESSION_MAX_AGE)
            return data.get("authenticated", False)
        except Exception:
            return False
