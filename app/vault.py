"""JSON-backed secret vault. Stores API keys globally so all installed apps
can share them. File is chmod 600 (owner-only) on disk.

Ported from shirim-v2/vault.py with minor additions (list_masked, check).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VAULT_DIR = Path("~/.shirim").expanduser()
VAULT_FILE = VAULT_DIR / "secrets.json"


def _ensure() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, str]:
    if not VAULT_FILE.exists():
        return {}
    try:
        return json.loads(VAULT_FILE.read_text())
    except Exception:
        return {}


def save(secrets: dict[str, str]) -> None:
    _ensure()
    VAULT_FILE.write_text(json.dumps(secrets, indent=2))
    try:
        os.chmod(VAULT_FILE, 0o600)
    except Exception:
        pass


def get(name: str) -> str | None:
    return load().get(name)


def set_key(name: str, value: str) -> None:
    s = load()
    s[name] = value
    save(s)


def delete_key(name: str) -> bool:
    s = load()
    if name not in s:
        return False
    del s[name]
    save(s)
    return True


def mask(value: str) -> str:
    """Mask a secret for display: sk-proj-dDch...xt8A"""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (min(len(value) - 8, 12)) + value[-4:]


def list_masked() -> list[dict]:
    """Return all secrets as {name, masked_value, length} — safe to send to frontend."""
    return [
        {"name": k, "masked_value": mask(v), "length": len(v)}
        for k, v in sorted(load().items())
    ]


def check(names: list[str]) -> dict[str, bool]:
    """Given a list of env var names, return which ones are present in the vault."""
    secrets = load()
    return {n: (n in secrets and bool(secrets[n])) for n in names}
