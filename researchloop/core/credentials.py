"""Local CLI credentials for connecting to a remote orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

_CREDENTIALS_PATH = Path.home() / ".config" / "researchloop" / "credentials.json"


def save_credentials(url: str, shared_secret: str) -> Path:
    """Save orchestrator credentials to disk."""
    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_PATH.write_text(
        json.dumps(
            {"url": url, "shared_secret": shared_secret},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _CREDENTIALS_PATH.chmod(0o600)
    return _CREDENTIALS_PATH


def load_credentials() -> dict[str, str] | None:
    """Load saved credentials, or None if not configured."""
    if not _CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if data.get("url") and data.get("shared_secret"):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def clear_credentials() -> None:
    """Remove saved credentials."""
    if _CREDENTIALS_PATH.exists():
        _CREDENTIALS_PATH.unlink()
