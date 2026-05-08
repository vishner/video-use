"""Shared environment / .env loader for video-use helpers.

Single source of truth for resolving secrets like ELEVENLABS_API_KEY.

Resolution order:
  1. Process environment variable
  2. .env in the repo root (one level up from helpers/)
  3. .env in the current working directory

Lines in .env follow the simple ``KEY=VALUE`` format. Values may be wrapped in
single or double quotes. Lines starting with ``#`` are ignored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file. Returns {} if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            result[k] = v
    return result


def get_env(key: str, default: str | None = None) -> str | None:
    """Look up an environment variable from process env, repo .env, or cwd .env."""
    if key in os.environ and os.environ[key]:
        return os.environ[key]
    for candidate in (REPO_ROOT / ".env", Path.cwd() / ".env"):
        values = _parse_env_file(candidate)
        if values.get(key):
            return values[key]
    return default


def require_env(key: str, hint: str | None = None) -> str:
    """Get an env var or exit with a clear message."""
    v = get_env(key)
    if not v:
        msg = f"{key} not found in environment or .env"
        if hint:
            msg += f" -- {hint}"
        sys.exit(msg)
    return v


def load_elevenlabs_key() -> str:
    return require_env(
        "ELEVENLABS_API_KEY",
        hint="paste a key from https://elevenlabs.io/app/settings/api-keys "
             "into .env at the repo root",
    )
