"""Google Calendar import engine (phase 1) and push helpers (phase 2).

The engine talks only through the injected client and store instances, so tests
run without network access. Imported events are origin 'google' and never carry
a handoff. Google text is treated as untrusted data.
"""
from __future__ import annotations

import datetime

FULL_WINDOW_PAST = 90
FULL_WINDOW_FUTURE = 365


def _local_date_from_google(google_event):
    start = google_event.get('start') or {}
    if 'date' in start:
        return start['date']
    dt = start.get('dateTime', '')
    return dt[:10] if dt else ''


def _parse_time(value):
    """Return HH:MM from an RFC3339 dateTime, or '' for all-day."""
    if not isinstance(value, str) or 'T' not in value:
        return ''
    time_part = value.split('T', 1)[1]
    if time_part.endswith('Z'):
        time_part = time_part[:-1]
    return time_part[:5]


def _extract_rrule(google_event):
    for entry in google_event.get('recurrence') or []:
        if isinstance(entry, str) and entry.startswith('RRULE:'):
            return entry[len('RRULE:'):]
    return ''


class GoogleSyncEngine:
    def __init__(self, store, client, mapping, calendar_metadata=None):
        self.store = store
        self.client = client
        self.mapping = mapping
        self.calendar_metadata = calendar_metadata or {}

    def _existing_calendar(self, calendar_id):
        if getattr(self, '_existing_calendars_cache', None) is None:
            try:
                calendars = (self.store.get() or {}).get('calendars') or {}
            except Exception:
                calendars = {}
            self._existing_calendars_cache = {
                calendar.get('google_calendar_id'): calendar
                for calendar in calendars.values()
                if isinstance(calendar, dict) and calendar.get('google_calendar_id')
            }
        return self._existing_calendars_cache.get(calendar_id)

    def _local_calendar_id(self, calendar_id):
        metadata = self.calendar_metadata.get(calendar_id) or {}
        discovered_name = str(metadata.get('summary') or '').strip()
        name = discovered_name or None
        access_role = str(metadata.get('accessRole') or '').strip().lower()
        existing = self._existing_calendar(calendar_id)
        existing_name = existing.get('name') if existing else None
        existing_writable = existing.get('writable') if existing else None
        # Preserve a previously discovered user-specific name when a
        # later sync cannot discover metadata. Legacy fallbacks apply
        # only when this Google calendar has never been seen locally.
        if not name and existing_name:
            name = existing_name
        if access_role:
            writable = access_role in {'owner', 'writer'}
        elif existing is not None and existing_writable is not None:
            # Missing accessRole must not silently downgrade writability,
            # even when a summary is present.
            writable = existing_writable
        elif calendar_id == 'primary':
            writable = True
        else:
            writable = False
        if not name:
            if calendar_id == 'primary':
                name = 'Google Primary'
            elif 'holiday' in str(calendar_id).lower():
                name = 'US Holidays'
            else:
                name = 'Google Calendar'
        return self.store.ensure_google_calendar(calendar_id, name, writable)

    def _google_to_local(self, google_event, calendar_id):
        """Convert one Google event to (date_str, local_event, zone, google_id, etag)."""
        start = google_event.get('start') or {}
        end = google_event.get('end') or {}
        all_day = 'date' in start and 'dateTime' not in start
        zone = start.get('timeZone') or end.get('timeZone') or ''
        date_str = _local_date_from_google(google_event)

        if all_day:
            time_val = ''
            until = ''
        else:
            time_val = _parse_time(start.get('dateTime', ''))
            until = _parse_time(end.get('dateTime', ''))

        title = str(google_event.get('summary') or '').strip()[:200]
        if not title:
            title = '(untitled)'
        desc = google_event.get('description')
        if not isinstance(desc, str):
            desc = ''
        desc = desc[:2000]

        event = {
            'title': title,
            'time': time_val,
            'until': until,
            'category': '',
            'desc': desc,
            'origin': 'google',
            'calendar': self._local_calendar_id(calendar_id),
        }
        rrule = _extract_rrule(google_event)
        if rrule:
            event['rrule'] = rrule
        event['google_id'] = google_event.get('id') or ''
        # id assigned by store on add; updates reuse existing id via mapping
        return date_str, event, zone, google_event.get('id'), google_event.get('etag', '')

    def _date_window(self, now):
        past = now - datetime.timedelta(days=FULL_WINDOW_PAST)
        future = now + datetime.timedelta(days=FULL_WINDOW_FUTURE)
        return past.isoformat(), future.isoformat()

    def import_calendar(self, calendar_id, now=None):
        # Register/refresh the local calendar even if it has no events in
        # the fetched window, so discovered names are not lost for empty
        # calendars.
        self._local_calendar_id(calendar_id)
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        sync_token = self.mapping.get_sync_token(calendar_id)
        report = {'imported': 0, 'updated': 0, 'removed': 0, 'errors': []}
        full_import = not sync_token

        if sync_token:
            items, next_token, gone = self._fetch(calendar_id, sync_token=sync_token)
            if gone:
                # 410: token invalid, clear and do a full resync
                self.mapping.clear_sync_token(calendar_id)
                sync_token = None
                full_import = True
                items, next_token, gone = self._fetch(calendar_id, now=now)
        else:
            items, next_token, gone = self._fetch(calendar_id, now=now)

        # Process events
        for g_event in items:
            try:
                self._process_one(g_event, calendar_id, report)
            except Exception as exc:
                report['errors'].append(str(exc))

        if full_import:
            self._reconcile_missing(calendar_id, items, now, report)

        if next_token:
            self.mapping.set_sync_token(calendar_id, next_token)
        return report

    def import_window(self, calendar_id, time_min, time_max, now=None):
        # One-off explicit window fetch. Does not read, clear, or write the sync token,
        # and never reconciles (removes) local events.
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        report = {'imported': 0, 'updated': 0, 'removed': 0, 'errors': []}
        result = self.client.list_events(
            calendar_id, time_min=time_min, time_max=time_max)
        items = result.get('items', []) if isinstance(result, dict) else []
        for g_event in items:
            try:
                self._process_one(g_event, calendar_id, report)
            except Exception as exc:
                report['errors'].append(str(exc))
        return report

    def _reconcile_missing(self, calendar_id, items, now, report):
        """Remove mapped local events absent from a complete fetched window.

        Incremental responses are deltas and never call this method. Events
        outside the full-import window are retained because they were not part
        of the server snapshot.
        """
        window_start = (now.date() - datetime.timedelta(days=FULL_WINDOW_PAST)).isoformat()
        window_end = (now.date() + datetime.timedelta(days=FULL_WINDOW_FUTURE)).isoformat()
        seen_google_ids = {
            event.get('id') for event in items
            if isinstance(event, dict) and event.get('id')
        }
        stored = self.store.get().get('events', {})
        local_dates = {
            event.get('id'): date_str
            for date_str, day in stored.items()
            for event in day
            if event.get('id')
        }
        for event_id, record in self.mapping.get_mappings().items():
            if record.get('calendar_id') != calendar_id:
                continue
            if record.get('google_id') in seen_google_ids:
                continue
            date_str = local_dates.get(event_id)
            if not date_str or not (window_start <= date_str <= window_end):
                continue
            if self.store.delete_by_id(event_id, allow_readonly=True):
                self.mapping.remove(event_id)
                report['removed'] += 1

    def _fetch(self, calendar_id, sync_token=None, now=None):
        """Return (items, next_sync_token, gone_flag)."""
        try:
            if sync_token:
                result = self.client.list_events(calendar_id, sync_token=sync_token)
            else:
                time_min, time_max = self._date_window(now)
                result = self.client.list_events(
                    calendar_id, time_min=time_min, time_max=time_max)
        except Exception as exc:
            status = getattr(getattr(exc, 'resp', None), 'status', None)
            if status == 410:
                return [], None, True
            raise
        items = result.get('items', []) if isinstance(result, dict) else []
        next_token = result.get('nextSyncToken') if isinstance(result, dict) else None
        return items, next_token, False

    def _process_one(self, g_event, calendar_id, report):
        if g_event.get('recurringEventId'):
            # Instance of a recurring series: the master already represents it.
            return
        google_id = g_event.get('id')
        status = g_event.get('status')
        evt_id = self.mapping.find_by_google(calendar_id, google_id)

        if status == 'cancelled':
            if evt_id:
                self.store.delete_by_id(evt_id, allow_readonly=True)
                self.mapping.remove(evt_id)
                report['removed'] += 1
            return

        date_str, local_event, zone, _gid, etag = self._google_to_local(
            g_event, calendar_id)

        existing_local = None
        if evt_id:
            for day in self.store.get().get('events', {}).values():
                for candidate in day:
                    if candidate.get('id') == evt_id:
                        existing_local = candidate
                        break
                if existing_local is not None:
                    break

        if evt_id:
            # Google owns event fields, but a handoff explicitly configured in
            # Mech.Calendar is local metadata and must survive refreshes.
            if isinstance(existing_local, dict) and 'handoff' in existing_local:
                local_event['handoff'] = existing_local['handoff']
            # update existing event
            local_event['id'] = evt_id
            if not self.store.update_event(date_str, local_event, allow_readonly=True):
                # event not found in store; re-add
                self.store.add(date_str, local_event, allow_readonly=True)
            self.mapping.put({
                'evt_id': evt_id, 'google_id': google_id,
                'calendar_id': calendar_id, 'etag': etag,
                'zone': zone,
                'last_synced': datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
            })
            report['updated'] += 1
        else:
            existing = None
            for cand in self.store.get().get('events', {}).get(date_str, []):
                if cand.get('origin') == 'google' and cand.get('google_id') == google_id:
                    existing = cand
                    break
            if existing is not None:
                if 'handoff' in existing:
                    local_event['handoff'] = existing['handoff']
                local_event['id'] = existing['id']
                self.store.update_event(date_str, local_event, allow_readonly=True)
                self.mapping.put({
                    'evt_id': existing['id'], 'google_id': google_id,
                    'calendar_id': calendar_id, 'etag': etag,
                    'zone': zone,
                    'last_synced': datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                })
                report['updated'] += 1
                return
            index = self.store.add(date_str, local_event, allow_readonly=True)
            # retrieve the assigned id
            day = self.store.get()['events'].get(date_str, [])
            new_id = day[index]['id'] if index < len(day) else local_event.get('id')
            self.mapping.put({
                'evt_id': new_id, 'google_id': google_id,
                'calendar_id': calendar_id, 'etag': etag,
                'zone': zone,
                'last_synced': datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
            })
            report['imported'] += 1

    def _local_to_google(self, ev, date_str, zone=''):
        body = {'summary': ev.get('title', ''), 'description': ev.get('desc', '')}
        if ev.get('time'):
            end_time = ev.get('until') or ev['time']
            start = {'dateTime': date_str + 'T' + ev['time'] + ':00'}
            end = {'dateTime': date_str + 'T' + end_time + ':00'}
            if zone:
                start['timeZone'] = zone
                end['timeZone'] = zone
            body['start'] = start
            body['end'] = end
        else:
            day = datetime.date.fromisoformat(date_str)
            body['start'] = {'date': day.isoformat()}
            body['end'] = {'date': (day + datetime.timedelta(days=1)).isoformat()}
        if ev.get('rrule'):
            body['recurrence'] = ['RRULE:' + ev['rrule']]
        return body

    def _now_iso(self):
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def push_create(self, calendar_id, date_str, ev):
        report = {'pushed': 0, 'conflicts': 0, 'errors': []}
        body = self._local_to_google(ev, date_str)
        try:
            res = self.client.insert_event(calendar_id, body)
        except Exception as exc:
            report['errors'].append(str(exc))
            return report
        self.mapping.put({
            'evt_id': ev['id'], 'google_id': res.get('id'),
            'calendar_id': calendar_id, 'etag': res.get('etag', ''),
            'zone': '', 'last_synced': self._now_iso(),
        })
        report['pushed'] = 1
        return report

    def push_update(self, calendar_id, date_str, ev):
        report = {'pushed': 0, 'conflicts': 0, 'errors': []}
        rec = self.mapping.get(ev['id'])
        if not rec:
            return self.push_create(calendar_id, date_str, ev)
        body = self._local_to_google(ev, date_str, rec.get('zone', ''))
        try:
            res = self.client.patch_event(
                calendar_id, rec['google_id'], body, etag=rec.get('etag'))
        except Exception as exc:
            status = getattr(getattr(exc, 'resp', None), 'status', None)
            if status == 412:
                # take-Google: keep mapping unchanged so the next import pulls the Google version
                report['conflicts'] = 1
            else:
                report['errors'].append(str(exc))
            return report
        rec['etag'] = (res or {}).get('etag', rec.get('etag', ''))
        rec['last_synced'] = self._now_iso()
        self.mapping.put(rec)
        report['pushed'] = 1
        return report

    def push_delete(self, calendar_id, evt_id):
        report = {'pushed': 0, 'conflicts': 0, 'errors': []}
        rec = self.mapping.get(evt_id)
        if not rec:
            return report
        try:
            self.client.delete_event(calendar_id, rec['google_id'])
        except Exception as exc:
            report['errors'].append(str(exc))
            return report
        self.mapping.remove(evt_id)
        report['pushed'] = 1
        return report
