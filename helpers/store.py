"""Mech.Calendar event store — schema v6.

v6 replaces fixed category presets with a user-defined labels registry.
Events keep the `category` field, but its value is now a label id or empty
(unlabeled). Legacy v5 stores migrate distinct non-empty category values
into labels. v5 added a first-class calendar registry; v3 added stable event
ids, origin, until and handoff; v2 files auto-migrate on load with a
one-time .v2.bak.json backup.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - the framework runs on Unix
    fcntl = None

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
ID_RE = re.compile(r'^evt_[0-9a-f]{12}$')
LABEL_ID_RE = re.compile(r'^lbl_[0-9a-f]{12}$')
DESC_MAX = 2000
TITLE_MAX = 200
HANDOFF_PROMPT_MAX = 2000
HANDOFF_LEAD_MAX = 1440  # one day; anything beyond is treated as unset
STORE_VERSION = 6
LABEL_NAME_MAX = 50
ORIGINS = ('ui', 'agent', 'google')
HANDOFF_AT = ('start', 'finish', 'both')
DEFAULT_CALENDAR_ID = 'cal_local'
DEFAULT_CALENDAR = {
    'id': DEFAULT_CALENDAR_ID,
    'name': 'Personal',
    'color': '#34c759',
    'visible': True,
    'writable': True,
    'source': 'local',
    'google_calendar_id': None,
    'order': 0,
}


def default_calendars():
    """Return a fresh copy of the default calendar registry."""
    return {DEFAULT_CALENDAR_ID: dict(DEFAULT_CALENDAR)}


def default_labels():
    """Return a fresh (empty) user-label registry."""
    return []


def _label_id(name: str) -> str:
    """Return a stable non-secret identifier for a user label name."""
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
    return 'lbl_' + digest


def labels_from_events(events):
    """Derive user labels from legacy event category values in first-seen order."""
    labels = []
    seen = set()
    for day in (events or {}).values():
        for event in day or []:
            name = event.get('category') if isinstance(event, dict) else ''
            if not isinstance(name, str):
                continue
            name = name.strip()[:LABEL_NAME_MAX]
            if not name or name == 'default' or name in seen:
                continue
            seen.add(name)
            labels.append({
                'id': _label_id(name),
                'name': name,
                'color': '#8e8e93',
            })
    return labels


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


RRULE_MAX = 200
RRULE_FREQ = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
RRULE_KEYS = ('FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY')
RRULE_DAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
RRULE_UNTIL_RE = re.compile(r'^\d{8}(T\d{6}Z)?$')


def _clean_rrule(raw):
    """Return a normalized conservative RFC 5545 subset, or None if invalid.

    Supports FREQ (required), INTERVAL, COUNT, UNTIL and BYDAY only.
    Anything else is dropped so a malformed value never breaks the event.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > RRULE_MAX:
        return None
    parts = {}
    for piece in text.split(';'):
        key, sep, value = piece.partition('=')
        if not sep or key not in RRULE_KEYS or key in parts:
            return None
        parts[key] = value
    if parts.get('FREQ') not in RRULE_FREQ:
        return None
    if 'INTERVAL' in parts and not (parts['INTERVAL'].isdigit() and 1 <= int(parts['INTERVAL']) <= 999):
        return None
    if 'COUNT' in parts and not (parts['COUNT'].isdigit() and 1 <= int(parts['COUNT']) <= 9999):
        return None
    if 'UNTIL' in parts and not RRULE_UNTIL_RE.match(parts['UNTIL']):
        return None
    if 'BYDAY' in parts:
        days = parts['BYDAY'].split(',')
        if not days or any(d not in RRULE_DAYS for d in days):
            return None
    return text


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
    """JSON file store {version:5, rev:int, calendars:{...}, events:{...}} with
    atomic writes. rev increments on every persisted mutation; replace()
    optionally rejects writes from writers holding an older revision."""

    def __init__(self, path=None):
        self.path = path or plugin_store_path()
        self.lock = threading.Lock()
        self._data = {'version': STORE_VERSION, 'rev': 1,
                          'calendars': default_calendars(), 'labels': default_labels(),
                          'events': {}}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._file_lock():
            self._load()

    def _v2_backup_path(self) -> str:
        return re.sub(r'\.json$', '.v2.bak.json', self.path)

    def _preserve_unreadable(self):
        """Copy an invalid store file aside before it is replaced, so
        original bytes are never silently lost."""
        try:
            if not os.path.exists(self.path):
                return
            stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
            backup = re.sub(r'\.json$', f'.corrupt-{stamp}.json', self.path)
            shutil.copy2(self.path, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
        except OSError:
            pass  # preservation must never break load

    def _reset_invalid(self):
        self._preserve_unreadable()
        self._data = {'version': STORE_VERSION, 'rev': 1,
                      'calendars': default_calendars(),
                      'labels': default_labels(), 'events': {}}

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            self._preserve_unreadable()
            self._data = {'version': STORE_VERSION, 'rev': 1,
                          'calendars': default_calendars(),
                          'labels': default_labels(), 'events': {}}
            return
        if not (isinstance(data, dict) and isinstance(data.get('events'), dict)):
            self._reset_invalid()
            return
        version = data.get('version', 2)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1 or version > STORE_VERSION:
            self._reset_invalid()
            return
        events = data['events']
        rev = data.get('rev', 1)
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            rev = 1
        if version < STORE_VERSION:
            backup = self._v2_backup_path()
            try:
                if not os.path.exists(backup):
                    shutil.copy2(self.path, backup)
            except OSError:
                pass
            labels = labels_from_events(events)
            events = self._migrate_events(events)
            calendars = data.get('calendars')
            if not isinstance(calendars, dict):
                calendars = default_calendars()
            self._data = {'version': STORE_VERSION, 'rev': rev,
                          'calendars': calendars, 'labels': labels,
                          'events': events}
            self._persist()
        else:
            calendars = data.get('calendars')
            if not isinstance(calendars, dict):
                calendars = default_calendars()
            labels = data.get('labels')
            if not isinstance(labels, list):
                labels = labels_from_events(events)
            # Normalize legacy built-in 'default' in already-v6 stores:
            # 'default' means unlabeled, never a user label.
            normalized_labels = [
                label for label in labels
                if not (isinstance(label, dict) and label.get('name') == 'default')
            ]
            existing_names = {
                label.get('name') for label in normalized_labels
                if isinstance(label, dict)
            }
            for derived in labels_from_events(events):
                if derived['name'] not in existing_names:
                    normalized_labels.append(derived)
                    existing_names.add(derived['name'])
            changed = normalized_labels != labels
            labels = normalized_labels
            for day in events.values():
                if not isinstance(day, list):
                    continue
                for event in day:
                    if isinstance(event, dict) and event.get('category') == 'default':
                        event['category'] = ''
                        changed = True
            self._data = {'version': STORE_VERSION, 'rev': rev,
                          'calendars': calendars, 'labels': labels,
                          'events': events}
            if changed:
                self._persist()

    def _migrate_events(self, events):
        """Pre-v6 -> v6: sanitize and assign the default calendar.

        Legacy category text is retained as the user's label value. The old
        built-in ``default`` category is the only value that means unlabeled.
        """
        clean = sanitize_store(events)
        for day in clean.values():
            for event in day:
                event['calendar'] = DEFAULT_CALENDAR_ID
                if event.get('category') == 'default':
                    event['category'] = ''
        return clean

    @contextmanager
    def _file_lock(self):
        """Serialize snapshots and atomic writes across store instances."""
        lock_path = self.path + '.lock'
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, 'a+', encoding='utf-8') as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reload_locked(self):
        """Refresh the in-memory snapshot; caller holds self.lock and file lock."""
        self._load()

    def _persist(self):
        self._data['rev'] = int(self._data.get('rev', 1)) + 1
        directory = os.path.dirname(self.path) or '.'
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
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @contextmanager
    def _mutation_lock(self):
        """Reload and mutate one current snapshot atomically across processes."""
        with self.lock:
            with self._file_lock():
                self._reload_locked()
                yield

    def get(self):
        with self.lock:
            with self._file_lock():
                self._reload_locked()
                return json.loads(json.dumps(self._data))  # deep copy

    def add(self, date_str, event, allow_readonly=False):
        with self._mutation_lock():
            event, err = sanitize_event(event)
            if err:
                raise ValueError(err)
            self._require_calendar_locked(event['calendar'])
            if not allow_readonly:
                self._require_writable_calendar_locked(event['calendar'])
            day = self._data['events'].setdefault(date_str, [])
            day.append(event)
            index = len(day) - 1
            self._persist()
            return index

    def replace(self, events, base_rev=None, allow_readonly=False):
        with self._mutation_lock():
            if base_rev is not None and base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')
            if not isinstance(events, dict):
                raise ValueError('events must be an object')
            for day in events.values():
                if not isinstance(day, list):
                    raise ValueError('events must contain lists')
                for event in day:
                    if not isinstance(event, dict):
                        raise ValueError('event must be an object')
                    self._require_calendar_locked(
                        event.get('calendar', DEFAULT_CALENDAR_ID))
                    if not allow_readonly:
                        self._require_writable_event_locked(event)
            if not allow_readonly:
                current_by_id = {
                    event.get('id'): (date_str, event)
                    for date_str, day in self._data['events'].items()
                    for event in day
                    if event.get('id')
                }
                replacement_by_id = {
                    event.get('id'): (date_str, event)
                    for date_str, day in events.items()
                    for event in day
                    if event.get('id')
                }
                for event_id, current in current_by_id.items():
                    date_str, event = current
                    if self._data['calendars'].get(
                            event.get('calendar', DEFAULT_CALENDAR_ID),
                            {}).get('writable', False):
                        continue
                    if replacement_by_id.get(event_id) != current:
                        raise ValueError('calendar is read-only')
            self._data = {'version': STORE_VERSION,
                          'rev': self._data.get('rev', 1),
                          'calendars': self._data.get('calendars') or default_calendars(),
                          'labels': self._data.get('labels') or default_labels(),
                          'events': events}
            self._persist()

    def delete_date(self, date_str, base_rev=None, allow_readonly=False):
        with self._mutation_lock():
            if base_rev is not None and base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')
            day = self._data['events'].get(date_str)
            if day is None:
                return False
            if not allow_readonly:
                for event in day:
                    self._require_writable_event_locked(event)
            del self._data['events'][date_str]
            self._persist()
            return True

    def delete_index(self, date_str, index):
        with self._mutation_lock():
            day = self._data['events'].get(date_str)
            if not day or not (0 <= index < len(day)):
                return False
            self._require_writable_event_locked(day[index])
            del day[index]
            if not day:
                del self._data['events'][date_str]
            self._persist()
            return True

    def delete_by_id(self, eid, allow_readonly=False):
        """Remove the event with this id from anywhere in the store."""
        if not _valid_id(eid):
            return False
        with self._mutation_lock():
            for date_str, day in self._data['events'].items():
                for i, ev in enumerate(day):
                    if ev.get('id') == eid:
                        if not allow_readonly:
                            self._require_writable_event_locked(ev)
                        del day[i]
                        if not day:
                            del self._data['events'][date_str]
                        self._persist()
                        return True
        return False

    def update_event(self, date_str, event, allow_readonly=False):
        """Replace the event whose id matches event['id']; may move dates."""
        eid = event.get('id') if isinstance(event, dict) else None
        if not _valid_id(eid):
            return False
        with self._mutation_lock():
            target_calendar = event.get('calendar', DEFAULT_CALENDAR_ID)
            self._require_calendar_locked(target_calendar)
            if not allow_readonly:
                self._require_writable_calendar_locked(target_calendar)
            found_date = None
            existing_event = None
            for d, day in self._data['events'].items():
                for existing in day:
                    if existing.get('id') == eid:
                        found_date = d
                        existing_event = existing
                        break
                if found_date is not None:
                    break
            if found_date is None:
                return False
            if not allow_readonly:
                self._require_writable_event_locked(existing_event)
            day = self._data['events'][found_date]
            day[:] = [ev for ev in day if ev.get('id') != eid]
            if not day:
                del self._data['events'][found_date]
            self._data['events'].setdefault(date_str, []).append(event)
            self._persist()
            return True

    def _require_calendar_locked(self, calendar_id):
        if calendar_id not in self._data.get('calendars', {}):
            raise ValueError('unknown calendar')

    def ensure_google_calendar(self, google_calendar_id, name, writable):
        """Return a stable local calendar id for a Google calendar.

        Google-owned fields are created and maintained by the sync engine, not
        by the client-facing calendar settings action.
        """
        if not isinstance(google_calendar_id, str) or not google_calendar_id.strip():
            raise ValueError('google calendar id required')
        google_calendar_id = google_calendar_id.strip()
        with self._mutation_lock():
            calendars = self._data.setdefault('calendars', default_calendars())
            for calendar_id, calendar in calendars.items():
                if calendar.get('google_calendar_id') == google_calendar_id:
                    changed = False
                    safe_name = str(name or '').strip()[:100]
                    if safe_name and calendar.get('name') != safe_name:
                        calendar['name'] = safe_name
                        changed = True
                    if writable is not None:
                        writable_value = bool(writable)
                        if calendar.get('writable') != writable_value:
                            calendar['writable'] = writable_value
                            changed = True
                    if changed:
                        self._persist()
                    return calendar_id
            digest = hashlib.sha256(google_calendar_id.encode('utf-8')).hexdigest()[:12]
            calendar_id = 'cal_google_' + digest
            while calendar_id in calendars:
                digest = hashlib.sha256((google_calendar_id + calendar_id).encode('utf-8')).hexdigest()[:12]
                calendar_id = 'cal_google_' + digest
            calendars[calendar_id] = {
                'id': calendar_id,
                'name': str(name or 'Google Calendar')[:100],
                'color': '#0a84ff',
                'visible': True,
                'writable': bool(writable),
                'source': 'google',
                'google_calendar_id': google_calendar_id,
                'order': len(calendars),
            }
            self._persist()
            return calendar_id


    def _require_writable_calendar_locked(self, calendar_id):
        self._require_calendar_locked(calendar_id)
        if not self._data['calendars'][calendar_id].get('writable', False):
            raise ValueError('calendar is read-only')

    def _require_writable_event_locked(self, event):
        calendar_id = event.get('calendar', DEFAULT_CALENDAR_ID)
        self._require_writable_calendar_locked(calendar_id)

    def _calendars_with_counts_locked(self):
        """Registry copy with counts; caller must hold the store lock."""
        calendars = json.loads(json.dumps(self._data.get('calendars', {})))
        counts = {calendar_id: 0 for calendar_id in calendars}
        for day in self._data.get('events', {}).values():
            for event in day:
                calendar_id = event.get('calendar', DEFAULT_CALENDAR_ID)
                if calendar_id in counts:
                    counts[calendar_id] += 1
        for calendar_id, calendar in calendars.items():
            calendar['count'] = counts.get(calendar_id, 0)
        return calendars

    def calendars_with_counts(self):
        """Return a copy of the registry with counts derived from events."""
        with self.lock:
            with self._file_lock():
                self._reload_locked()
                return self._calendars_with_counts_locked()

    def save_calendar_settings(self, updates, base_rev=None):
        """Persist client-owned calendar presentation fields only."""
        if not isinstance(updates, list):
            raise ValueError('calendars must be a list')
        with self._mutation_lock():
            if base_rev is not None and base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')
            calendars = json.loads(json.dumps(self._data.get('calendars', {})))
            for update in updates:
                self._apply_calendar_settings_locked(calendars, update)
            self._data['calendars'] = calendars
            self._persist()
            return self._calendars_with_counts_locked()

    def save_labels(self, updates, base_rev=None):
        """Replace the user label registry and reconcile event categories."""
        if not isinstance(updates, list):
            raise ValueError('labels must be a list')
        with self._mutation_lock():
            if base_rev is not None and base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')

            current = {
                label.get('id'): label
                for label in self._data.get('labels', [])
                if isinstance(label, dict) and label.get('id')
            }
            labels = []
            seen_ids = set()
            seen_names = set()
            renames = {}
            for raw in updates:
                if not isinstance(raw, dict):
                    raise ValueError('label must be an object')
                name = raw.get('name')
                if not isinstance(name, str) or not name.strip():
                    raise ValueError('label name required')
                name = name.strip()[:LABEL_NAME_MAX]
                if name == 'default':
                    raise ValueError('label name reserved')
                if name.casefold() in seen_names:
                    raise ValueError('duplicate label name')

                color = raw.get('color', '#8e8e93')
                if not isinstance(color, str) or not re.match(r'^#[0-9a-fA-F]{6}$', color):
                    raise ValueError('label color must be six-digit hex')

                label_id = raw.get('id')
                if not isinstance(label_id, str) or not LABEL_ID_RE.match(label_id):
                    label_id = _label_id(name)
                if label_id in seen_ids:
                    raise ValueError('duplicate label id')
                old = current.get(label_id)
                if old and old.get('name') != name:
                    renames[old.get('name')] = name

                seen_ids.add(label_id)
                seen_names.add(name.casefold())
                labels.append({
                    'id': label_id,
                    'name': name,
                    'color': color.lower(),
                })

            removed_names = {
                label.get('name')
                for label_id, label in current.items()
                if label_id not in seen_ids and label.get('name')
            }
            for day in self._data.get('events', {}).values():
                for event in day:
                    category = event.get('category', '')
                    if category in renames:
                        event['category'] = renames[category]
                    elif category in removed_names:
                        event['category'] = ''

            self._data['labels'] = labels
            self._persist()
            return json.loads(json.dumps(labels))

    def save_calendar_setting(self, update, base_rev):
        """Atomically save one calendar's presentation fields if revision matches."""
        with self._mutation_lock():
            if base_rev != self._data.get('rev'):
                raise StaleWriteError('store changed since read')
            calendars = json.loads(json.dumps(self._data.get('calendars', {})))
            self._apply_calendar_settings_locked(calendars, update)
            self._data['calendars'] = calendars
            self._persist()
            return self._calendars_with_counts_locked()

    @staticmethod
    def _apply_calendar_settings_locked(calendars, update):
        if not isinstance(update, dict):
            raise ValueError('calendar must be an object')
        calendar_id = update.get('id')
        if calendar_id not in calendars:
            raise ValueError('unknown calendar')
        if 'name' in update:
            name = update['name']
            if not isinstance(name, str) or not name.strip():
                raise ValueError('calendar name required')
            calendars[calendar_id]['name'] = name.strip()[:100]
        if 'color' in update:
            color = update['color']
            if not isinstance(color, str) or not re.match(r'^#[0-9a-fA-F]{6}$', color):
                raise ValueError('calendar color must be six-digit hex')
            calendars[calendar_id]['color'] = color.lower()
        if 'visible' in update:
            if not isinstance(update['visible'], bool):
                raise ValueError('calendar visible must be boolean')
            calendars[calendar_id]['visible'] = update['visible']
        if 'order' in update:
            order = update['order']
            if isinstance(order, bool) or not isinstance(order, int):
                raise ValueError('calendar order must be an integer')
            calendars[calendar_id]['order'] = order


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

    rrule = _clean_rrule(raw.get('rrule'))
    if rrule is None:
        rrule = ''

    until = raw.get('until')
    if not isinstance(until, str) or not TIME_RE.match(until):
        until = ''

    category = raw.get('category')
    if isinstance(category, str):
        category = category.strip()[:LABEL_NAME_MAX]
    else:
        category = ''

    event = {
        'id': eid,
        'title': title.strip()[:TITLE_MAX],
        'time': '',
        'until': until,
        'category': category,
        'desc': raw.get('desc')[:DESC_MAX] if isinstance(raw.get('desc'), str) else '',
        'origin': origin,
        'calendar': raw.get('calendar', DEFAULT_CALENDAR_ID),
    }
    if rrule:
        event['rrule'] = rrule
    gid = raw.get('google_id')
    if origin == 'google' and isinstance(gid, str) and gid.strip():
        event['google_id'] = gid.strip()[:200]
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
