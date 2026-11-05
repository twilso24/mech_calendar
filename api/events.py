"""Mech.Calendar plugin API handler (schema v3 + handoff sync + rev guard)."""
from flask import Request
from helpers.api import ApiHandler, Input, Output

from usr.plugins.mech_calendar.helpers.store import (
    EventStore, valid_date, sanitize_event, sanitize_store, ID_RE, _valid_id,
    StaleWriteError,
)
from usr.plugins.mech_calendar.helpers import sync
from usr.plugins.mech_calendar.helpers.rrule_expand import expand_occurrences
from usr.plugins.mech_calendar.helpers.google_map import MappingStore


_store: EventStore | None = None


PUSH_CALENDAR_ID = 'primary'


def _google_token():
    from usr.plugins.mech_calendar.helpers import google_auth
    return google_auth.load_token()


def _engine_call(op, *args):
    """Run one push operation through the Google sync engine. Never raises."""
    try:
        from usr.plugins.mech_calendar.api.google_sync import _make_client
        from usr.plugins.mech_calendar.helpers import google_sync
        from usr.plugins.mech_calendar.helpers.google_map import MappingStore
        engine = google_sync.GoogleSyncEngine(
            store=_get_store(), client=_make_client(_google_token()),
            mapping=MappingStore())
        return getattr(engine, op)(PUSH_CALENDAR_ID, *args)
    except Exception as exc:
        return {'pushed': 0, 'conflicts': 0, 'errors': [str(exc)]}


def _push_create(date_str, event):
    return _engine_call('push_create', date_str, event)


def _push_update(date_str, event):
    record = MappingStore().get(event.get('id')) if event.get('id') else None
    if record and record.get('calendar_id') != PUSH_CALENDAR_ID:
        return {'pushed': 0, 'conflicts': 0, 'errors': []}  # Holidays are read-only.
    return _engine_call('push_update', date_str, event)


def _push_delete(evt_id):
    record = MappingStore().get(evt_id)
    if record and record.get('calendar_id') != PUSH_CALENDAR_ID:
        return {'pushed': 0, 'conflicts': 0, 'errors': []}  # Holidays are read-only.
    return _engine_call('push_delete', evt_id)


def _push_deleted_events(events):
    """Push local deletions to Primary, never to read-only Holidays."""
    mapping = MappingStore()
    report = {'pushed': 0, 'conflicts': 0, 'errors': []}
    for event in events or []:
        event_id = event.get('id')
        if not event_id:
            continue
        record = mapping.get(event_id)
        if record and record.get('calendar_id') != PUSH_CALENDAR_ID:
            continue
        result = _push_delete(event_id)
        report['pushed'] += result.get('pushed', 0)
        report['conflicts'] += result.get('conflicts', 0)
        report['errors'].extend(result.get('errors', []))
    return report


def _merge_push(report, push):
    """Fold a push report into the sync report; no-op when not connected."""
    if push is None:
        return report
    merged = dict(report or {})
    merged['pushed'] = push.get('pushed', 0)
    merged['conflicts'] = push.get('conflicts', 0)
    merged['push_errors'] = push.get('errors', [])
    return merged


def _push_replace_changes(old_events, new_events):
    """Push primary-calendar changes made through the UI bulk-save path."""
    from usr.plugins.mech_calendar.helpers.google_map import MappingStore

    mapping = MappingStore()
    old_by_id = {
        event.get('id'): (date_str, event)
        for date_str, day in (old_events or {}).items()
        for event in day
        if event.get('id')
    }
    new_by_id = {
        event.get('id'): (date_str, event)
        for date_str, day in (new_events or {}).items()
        for event in day
        if event.get('id')
    }
    report = {'pushed': 0, 'conflicts': 0, 'errors': []}

    for event_id, (date_str, event) in new_by_id.items():
        record = mapping.get(event_id)
        if record and record.get('calendar_id') != PUSH_CALENDAR_ID:
            continue  # Holidays are read-only.
        if not record and event.get('origin') == 'google':
            continue  # Imported from Google: never re-create it there.
        try:
            if not record:
                result = _push_create(date_str, event)
            elif old_by_id.get(event_id) != (date_str, event):
                result = _push_update(date_str, event)
            else:
                continue
            report['pushed'] += result.get('pushed', 0)
            report['conflicts'] += result.get('conflicts', 0)
            report['errors'].extend(result.get('errors', []))
        except Exception as exc:
            report['errors'].append(str(exc))

    for event_id in old_by_id.keys() - new_by_id.keys():
        record = mapping.get(event_id)
        if not record or record.get('calendar_id') != PUSH_CALENDAR_ID:
            continue
        try:
            result = _push_delete(event_id)
            report['pushed'] += result.get('pushed', 0)
            report['conflicts'] += result.get('conflicts', 0)
            report['errors'].extend(result.get('errors', []))
        except Exception as exc:
            report['errors'].append(str(exc))
    return report


def _get_store() -> EventStore:
    global _store
    if _store is None:
        _store = EventStore()  # plugin-owned data/events.json
    return _store


def _protected_replace_error(old_events, new_events, calendars):
    """Reject client replacement changes to read-only events."""
    old_by_id = {
        event.get('id'): (date_str, event)
        for date_str, day in (old_events or {}).items()
        for event in day
        if event.get('id')
    }
    new_by_id = {
        event.get('id'): (date_str, event)
        for date_str, day in (new_events or {}).items()
        for event in day
        if event.get('id')
    }
    for event_id, old_item in old_by_id.items():
        old_date, old_event = old_item
        calendar = calendars.get(old_event.get('calendar', 'cal_local'), {})
        if calendar.get('writable', False):
            continue
        if new_by_id.get(event_id) != old_item:
            return 'calendar is read-only'
    return None


class EventsApi(ApiHandler):
    """Action dispatcher; every successful mutation reconciles handoffs.

    Responses carry the store revision (rev); replace() is guarded by it so
    a stale calendar surface reloads instead of overwriting newer state.
    """

    async def process(self, input: Input, request: Request) -> Output:
        action = input.get("action")

        if action == "list":
            data = _get_store().get()
            return {"ok": True, "rev": data["rev"], "events": data["events"],
                    "calendars": _get_store().calendars_with_counts(),
                    "labels": data.get("labels", [])}

        if action == "calendars":
            return {"ok": True, "calendars": _get_store().calendars_with_counts()}

        if action == "labels":
            return {"ok": True, "labels": _get_store().get().get("labels", [])}

        if action == "labels_save":
            base_rev = input.get("base_rev")
            if base_rev is None:
                return {"ok": False, "error": "revision required"}
            try:
                labels = _get_store().save_labels(
                    input.get("labels"), base_rev)
            except StaleWriteError:
                return {"ok": False, "error": "stale", "rev": _get_store().get()["rev"]}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "rev": _get_store().get()["rev"], "labels": labels}

        if action == "calendars_save":
            base_rev = input.get("base_rev")
            if base_rev is None:
                return {"ok": False, "error": "revision required"}
            try:
                calendars = _get_store().save_calendar_settings(
                    input.get("calendars"), base_rev)
            except StaleWriteError:
                return {"ok": False, "error": "stale", "rev": _get_store().get()["rev"]}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "rev": _get_store().get()["rev"], "calendars": calendars}

        if action == "calendar_save":
            try:
                calendars = _get_store().save_calendar_setting(
                    input.get("calendar"), input.get("base_rev"))
            except StaleWriteError:
                return {"ok": False, "error": "stale"}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "rev": _get_store().get()["rev"], "calendars": calendars}

        if action == "occurrences":
            window_start = input.get("window_start")
            window_end = input.get("window_end")
            if not valid_date(window_start) or not valid_date(window_end):
                return {"ok": False, "error": "invalid window"}
            store = _get_store()
            occurrences = []
            for date_str, day in store.get()["events"].items():
                for event in day:
                    rrule = event.get("rrule", "")
                    if not rrule:
                        continue
                    for occ_date in expand_occurrences(
                            rrule, date_str, window_start, window_end):
                        occ = {
                            'date': occ_date,
                            'title': event.get('title', ''),
                            'time': event.get('time', ''),
                            'until': event.get('until', ''),
                            'category': event.get('category', ''),
                            'desc': event.get('desc', ''),
                            'id': event.get('id', ''),
                            'series_id': event.get('id', ''),
                            'calendar': event.get('calendar', 'cal_local'),
                            'recurring': True,
                            'origin': event.get('origin', 'ui'),
                        }
                        occurrences.append(occ)
            occurrences.sort(key=lambda o: (o['date'], o['time']))
            return {"ok": True, "occurrences": occurrences}

        if action == "add":
            date_str = input.get("date")
            if not valid_date(date_str):
                return {"ok": False, "error": "invalid date"}
            event, err = sanitize_event(input.get("event"), strict_time=True)
            if err:
                return {"ok": False, "error": err}
            store = _get_store()
            try:
                index = store.add(date_str, event)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if event.get('handoff'):
                # explicit calendar edit with a handoff = re-arm intent
                sync.clear_suppression(store, [event['id']])
            report = await sync.sync_after_change(store=store)
            push = None
            if _google_token():
                push = _push_create(date_str, event)
            report = _merge_push(report, push)
            return {"ok": True, "rev": store.get()["rev"], "date": date_str,
                    "index": index, "id": event["id"], "sync": report}

        if action == "update":
            date_str = input.get("date")
            if not valid_date(date_str):
                return {"ok": False, "error": "invalid date"}
            raw_event = input.get("event")
            if not _valid_id(raw_event.get("id") if isinstance(raw_event, dict) else None):
                return {"ok": False, "error": "invalid id"}
            event, err = sanitize_event(raw_event, strict_time=True)
            if err:
                return {"ok": False, "error": err}
            store = _get_store()
            old_events = store.get()["events"]
            try:
                updated = store.update_event(date_str, event)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not updated:
                return {"ok": False, "error": "not found"}
            rearm = sync.rearm_ids(old_events, store.get()["events"])
            if rearm:
                # explicit calendar edit that adds/changes a handoff = re-arm intent
                sync.clear_suppression(store, rearm)
            report = await sync.sync_after_change(store=store)
            push = None
            if _google_token():
                push = _push_update(date_str, event)
            report = _merge_push(report, push)
            return {"ok": True, "rev": store.get()["rev"], "date": date_str,
                    "id": event["id"], "sync": report}

        if action == "replace":
            raw = input.get("events")
            if not isinstance(raw, dict):
                return {"ok": False, "error": "events must be an object"}
            canonical = sanitize_store(raw)
            store = _get_store()
            base_rev = input.get("base_rev")
            old_events = store.get()["events"]
            try:
                store.replace(canonical, base_rev=base_rev)
            except StaleWriteError:
                # a stale surface must reload, never overwrite newer state
                return {"ok": False, "error": "stale", "rev": store.get()["rev"]}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            rearm = sync.rearm_ids(old_events, store.get()["events"])

            if rearm:
                # explicit calendar edit that adds/changes a handoff = re-arm intent
                sync.clear_suppression(store, rearm)
            report = await sync.sync_after_change(store=store)
            if _google_token():
                report = _merge_push(report, _push_replace_changes(old_events, canonical))
            # canonical (id-assigned) events let the UI adopt stable ids
            return {"ok": True, "rev": store.get()["rev"], "events": canonical,
                    "sync": report}

        if action == "delete_date":
            base_rev = input.get("base_rev")
            if base_rev is None:
                return {"ok": False, "error": "revision required"}
            store = _get_store()
            removed_events = list(store.get()["events"].get(input.get("date"), []))
            try:
                deleted = store.delete_date(input.get("date"), base_rev=base_rev)
            except StaleWriteError:
                return {"ok": False, "error": "stale", "rev": store.get()["rev"]}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not deleted:
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            if _google_token():
                report = _merge_push(report, _push_deleted_events(removed_events))
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        if action == "delete_index":
            store = _get_store()
            date_str = input.get("date")
            index = input.get("index")
            day = store.get()["events"].get(date_str, [])
            removed_event = (
                day[index]
                if isinstance(index, int) and not isinstance(index, bool)
                and 0 <= index < len(day) else None
            )
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or False
            ):
                return {"ok": False, "error": "not found"}
            try:
                deleted = store.delete_index(date_str, index)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not deleted:
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            if _google_token() and removed_event:
                report = _merge_push(report, _push_deleted_events([removed_event]))
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        if action == "delete":
            if not _valid_id(input.get("id")):
                return {"ok": False, "error": "invalid id"}
            store = _get_store()
            try:
                deleted = store.delete_by_id(input.get("id"))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not deleted:
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            push = None
            if _google_token():
                push = _push_delete(input.get("id"))
            report = _merge_push(report, push)
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        return {"ok": False, "error": f"unknown action: {action}"}
