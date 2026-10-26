"""Mech.Calendar event store — schema v3.

v3 adds: stable event ids (evt_ + 12 hex), origin (ui|agent), until (finish-by
HH:MM), and optional handoff {at: start|finish|both, prompt, lead_minutes}
for scheduler integration. v2 files auto-migrate on load with a one-time
.v2.bak.json backup. Logic ported from the standalone server; validation
semantics unchanged for v2 fields.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
import threading

DEFAULT_CATEGORIES = ('default', 'work', 'personal', 'urgent', 'social',
                      'health', 'finance')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
ID_RE = re.compile(r'^evt_[0-9a-f]{12}$')
DESC_MAX = 2000
TITLE_MAX = 200
HANDOFF_PROMPT_MAX = 2000
HANDOFF_LEAD_MAX = 1440  # one day; anything beyond is treated as unset
STORE_VERSION = 3
ORIGINS = ('ui', 'agent')
HANDOFF_AT = ('start', 'finish', 'both')

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_store_path() -> str:
    """Plugin-owned default store path: <plugin>/data/events.json."""
    return os.path.join(_PLUGIN_DIR, 'data', 'events.json')


def new_event_id() -> str:
    return 'evt_' + secrets.token_hex(6)


def _valid_id(value):
    return isinstance(value, str) and bool(ID_RE.match(value))


def _clean_lead_minutes(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < 0 or value > HANDOFF_LEAD_MAX:
        return 0
    return value


def _clean_handoff(raw):
    """Normalize handoff or drop it entirely. Kept only when fully valid."""
    if not isinstance(raw, dict):
        return None
    at = raw.get('at')
    prompt = raw.get('prompt')
    if at not in HANDOFF_AT:
        return None
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return {
        'at': at,
        'prompt': prompt.strip()[:HANDOFF_PROMPT_MAX],
        'lead_minutes': _clean_lead_minutes(raw.get('lead_minutes')),
    }


class StaleWriteError(Exception):
    """Raised when a guarded write targets a store revision it never saw."""


class EventStore:
    """JSON file store {version:3, rev:int, events:{date:[event,...]}} with
    atomic writes. rev increments on every persisted mutation; replace()
    optionally rejects writes from writers holding an older revision."""

    def __init__(self, path=None):
        self.path = path or plugin_store_path()
        self.lock = threading.Lock()
        self._data = {'version': STORE_VERSION, 'rev': 1, 'events': {}}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._load()

    def _v2_backup_path(self) -> str:
        return re.sub(r'\.json$', '.v2.bak.json', self.path)

    def _preserve_unreadable(self):
        """Copy an unparseable store file aside before it is replaced, so
        original bytes are never silently lost."""
        try:
            if not os.path.exists(self.path):
                return
            stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
            backup = re.sub(r'\.json$', f'.corrupt-{stamp}.json', self.path)
            shutil.copy2(self.path, backup)
        except OSError:
            pass  # preservation must never break load

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            self._preserve_unreadable()
            self._data = {'version': STORE_VERSION, 'rev': 1, 'events': {}}
            return
        if not (isinstance(data, dict) and isinstance(data.get('events'), dict)):
            self._data = {'version': STORE_VERSION, 'rev': 1, 'events': {}}
            return
        events = data['events']
        rev = data.get('rev', 1)
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            rev = 1
        if data.get('version', 2) < STORE_VERSION:
            # one-time backup of the original v2 file, never overwritten
            backup = self._v2_backup_path()
            try:
                if not os.path.exists(backup):
                    shutil.copy2(self.path, backup)
            except OSError:
                pass  # migration proceeds even if backup fails
            events = self._migrate_events(events)
            self._data = {'version': STORE_VERSION, 'rev': rev, 'events': events}
            self._persist()
        else:
            self._data = {'version': STORE_VERSION, 'rev': rev, 'events': events}

    def _migrate_events(self, events):
        """v2 -> v3: re-sanitize (assigns ids, fills defaults)."""
        return sanitize_store(events)

    def _persist(self):
        self._data['rev'] = int(self._data.get('rev', 1)) + 1
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self._data))  # deep copy

    def add(self, date_str, event):
        with self.lock:
            event, err = sanitize_event(event)
            if err:
                raise ValueError(err)
            day = self._data['events'].setdefault(date_str, [])
            day.append(event)
            index = len(day) - 1
            self._persist()
            return index

    def replace(self, events, base_rev=None):
        with self.lock:
            if base_rev is not None and base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')
            self._data = {'version': STORE_VERSION,
                          'rev': self._data.get('rev', 1), 'events': events}
            self._persist()

    def delete_date(self, date_str):
        with self.lock:
            if date_str not in self._data['events']:
                return False
            del self._data['events'][date_str]
            self._persist()
            return True

    def delete_index(self, date_str, index):
        with self.lock:
            day = self._data['events'].get(date_str)
            if not day or not (0 <= index < len(day)):
                return False
            del day[index]
            if not day:
                del self._data['events'][date_str]
            self._persist()
            return True

    def delete_by_id(self, eid):
        """Remove the event with this id from anywhere in the store."""
        if not _valid_id(eid):
            return False
        with self.lock:
            for date_str, day in self._data['events'].items():
                for i, ev in enumerate(day):
                    if ev.get('id') == eid:
                        del day[i]
                        if not day:
                            del self._data['events'][date_str]
                        self._persist()
                        return True
        return False

    def update_event(self, date_str, event):
        """Replace the event whose id matches event['id']; may move dates."""
        eid = event.get('id') if isinstance(event, dict) else None
        if not _valid_id(eid):
            return False
        with self.lock:
            found_date = None
            for d, day in self._data['events'].items():
                if any(ev.get('id') == eid for ev in day):
                    found_date = d
                    break
            if found_date is None:
                return False
            day = self._data['events'][found_date]
            day[:] = [ev for ev in day if ev.get('id') != eid]
            if not day:
                del self._data['events'][found_date]
            self._data['events'].setdefault(date_str, []).append(event)
            self._persist()
            return True


def valid_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    y, m, d = (int(p) for p in s.split('-'))
    try:
        datetime.date(y, m, d)
    except ValueError:
        return False
    return True


def sanitize_event(raw, strict_time=False):
    """Return (event, error). strict_time: invalid time is an error (add/update);
    otherwise it is normalized to '' (bulk sanitize)."""
    if not isinstance(raw, dict):
        return None, 'event must be an object'
    title = raw.get('title')
    if not isinstance(title, str) or not title.strip():
        return None, 'title required'

    eid = raw.get('id')
    if not _valid_id(eid):
        eid = new_event_id()

    origin = raw.get('origin')
    if origin not in ORIGINS:
        origin = 'ui'

    until = raw.get('until')
    if not isinstance(until, str) or not TIME_RE.match(until):
        until = ''

    event = {
        'id': eid,
        'title': title.strip()[:TITLE_MAX],
        'time': '',
        'until': until,
        'category': raw.get('category') if raw.get('category') in DEFAULT_CATEGORIES else 'default',
        'desc': raw.get('desc')[:DESC_MAX] if isinstance(raw.get('desc'), str) else '',
        'origin': origin,
    }
    handoff = _clean_handoff(raw.get('handoff'))
    if handoff is not None:
        event['handoff'] = handoff

    time_val = raw.get('time')
    if time_val:  # empty/absent is allowed (all-day)
        if not isinstance(time_val, str) or not TIME_RE.match(time_val):
            if strict_time:
                return None, 'time must be 24h HH:MM'
            event['time'] = ''
        else:
            event['time'] = time_val
    return event, None


def sanitize_store(raw_events):
    """Bulk sanitize {date: [raw,...]} -> clean dict; invalid dates/events dropped."""
    clean = {}
    if not isinstance(raw_events, dict):
        return clean
    for date_str, day in raw_events.items():
        if not valid_date(date_str) or not isinstance(day, list):
            continue
        kept = []
        for raw in day:
            event, err = sanitize_event(raw)
            if err is None:
                kept.append(event)
        if kept:
            clean[date_str] = kept
    return clean
