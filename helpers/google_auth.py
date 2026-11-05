"""Google Calendar auth helpers: token storage and redacted status.

Tokens live outside the project checkout and the plugin store, with mode 600.
No Google SDK is imported here, so this module is safe to load in tests.
"""
from __future__ import annotations

import json
import os

TOKEN_FILENAME = 'google_calendar_token.json'
_SECRET_KEYS = ('refresh_token', 'access_token', 'client_secret', 'id_token')


def default_token_path() -> str:
    """Absolute token path under the user config dir, outside the repo."""
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'mech_calendar', TOKEN_FILENAME)


def save_token(token: dict, path: str | None = None) -> str:
    """Write the token dict atomically with mode 0600."""
    path = path or default_token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(token, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load_token(path: str | None = None) -> dict | None:
    """Return the stored token dict, or None if missing or unreadable."""
    path = path or default_token_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_token(path: str | None = None) -> None:
    """Remove the stored token. Safe when the file is absent."""
    path = path or default_token_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def public_status(token: dict | None) -> dict:
    """Connection status safe to return from the API: never includes secrets."""
    if not isinstance(token, dict):
        return {'connected': False}
    scopes = token.get('scopes')
    return {
        'connected': bool(token.get('refresh_token')),
        'scopes': [str(s) for s in scopes] if isinstance(scopes, list) else [],
    }
