"""Google sync sidecar mapping store: local evt_id to Google event identity.

Kept outside the event schema so the calendar UI and event store are unchanged.
Writes are atomic (tmp file then os.replace). A corrupt file loads as empty.
"""
from __future__ import annotations

import json
import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_map_path() -> str:
    """Plugin-owned sidecar path: <plugin>/data/google_sync.map.json."""
    return os.path.join(_PLUGIN_DIR, 'data', 'google_sync.map.json')


class MappingStore:
    def __init__(self, path: str | None = None):
        self.path = path or default_map_path()
        self._data = {'mappings': {}, 'sync_tokens': {}}
        self._load()

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get('mappings'), dict):
            self._data['mappings'] = data['mappings']
        if isinstance(data.get('sync_tokens'), dict):
            self._data['sync_tokens'] = data['sync_tokens']

    def _persist(self):
        directory = os.path.dirname(self.path) or '.'
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get_mappings(self) -> dict:
        return {k: dict(v) for k, v in self._data['mappings'].items()}

    def get(self, evt_id: str):
        rec = self._data['mappings'].get(evt_id)
        return dict(rec) if rec else None

    def put(self, rec: dict) -> None:
        if not isinstance(rec, dict) or not rec.get('evt_id'):
            raise ValueError('evt_id required')
        if not rec.get('google_id'):
            raise ValueError('google_id required')
        self._data['mappings'][rec['evt_id']] = dict(rec)
        self._persist()

    def remove(self, evt_id: str) -> None:
        if self._data['mappings'].pop(evt_id, None) is not None:
            self._persist()

    def find_by_google(self, calendar_id: str, google_id: str):
        for evt_id, rec in self._data['mappings'].items():
            if rec.get('calendar_id') == calendar_id and rec.get('google_id') == google_id:
                return evt_id
        return None

    def get_sync_token(self, calendar_id: str):
        return self._data['sync_tokens'].get(calendar_id)

    def set_sync_token(self, calendar_id: str, token: str) -> None:
        self._data['sync_tokens'][calendar_id] = token
        self._persist()

    def clear_sync_token(self, calendar_id: str) -> None:
        if self._data['sync_tokens'].pop(calendar_id, None) is not None:
            self._persist()
