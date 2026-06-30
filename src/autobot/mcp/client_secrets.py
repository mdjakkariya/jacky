"""Build-embedded OAuth client secrets for pre-registered apps (distribution).

End-users of a bundled Jack never enter a client secret: the developer registers one
OAuth app per provider and ships its secret in a gitignored JSON file
(``secrets/oauth_clients.json``), keyed by server id, e.g. ``{"github": "…", "slack": "…"}``.
In a source checkout the file lives at the repo root; in a PyInstaller bundle it is added as
data and read from the frozen bundle. A per-user Keychain entry (``mcp.<id>.client_secret``)
overrides this. Secrets are NEVER logged.
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

_SECRETS_FILENAME = "oauth_clients.json"


def _candidate_paths() -> list[Path]:
    """Where the embedded-secrets file may live, in priority order."""
    paths: list[Path] = []
    env = os.environ.get("JACK_OAUTH_SECRETS_FILE")
    if env:
        paths.append(Path(env).expanduser())
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle root
    if meipass:
        paths.append(Path(str(meipass)) / _SECRETS_FILENAME)
    # Source checkout: <repo>/secrets/oauth_clients.json (this file is src/autobot/mcp/…).
    paths.append(Path(__file__).resolve().parents[3] / "secrets" / _SECRETS_FILENAME)
    return paths


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    """Load the first readable secrets file from the candidate paths.

    Returns:
        A dict mapping server id to client secret string. Empty dict if no file is found
        or all candidates fail to parse.
    """
    for p in _candidate_paths():
        try:
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def default_client_secret(server_id: str) -> str | None:
    """The build-embedded client secret for ``server_id``, or ``None`` if absent.

    Args:
        server_id: The MCP server id to look up (e.g. ``"github"``).

    Returns:
        The embedded client secret string, or ``None`` if not found.
    """
    return _load().get(server_id) or None
