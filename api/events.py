"""Mech.Calendar plugin API handler (schema v3 + handoff sync + rev guard)."""
from flask import Request
from helpers.api import ApiHandler, Input, Output

from usr.plugins.mech_calendar.helpers.store import (
    EventStore, valid_date, sanitize_event, sanitize_store, ID_RE, _valid_id,
    StaleWriteError,
)
from usr.plugins.mech_calendar.helpers import sync

_store: EventStore | None = None


def _get_store() -> EventStore:
    global _store
    if _store is None:
        _store = EventStore()  # plugin-owned data/events.json
    return _store


class EventsApi(ApiHandler):
    """Action dispatcher; every successful mutation reconciles handoffs.

    Responses carry the store revision (rev); replace() is guarded by it so
    a stale calendar surface reloads instead of overwriting newer state.
    """

    async def process(self, input: Input, request: Request) -> Output:
        action = input.get("action")

        if action == "list":
            data = _get_store().get()
            return {"ok": True, "rev": data["rev"], "events": data["events"]}

        if action == "add":
            date_str = input.get("date")
            if not valid_date(date_str):
                return {"ok": False, "error": "invalid date"}
            event, err = sanitize_event(input.get("event"), strict_time=True)
            if err:
                return {"ok": False, "error": err}
            store = _get_store()
            index = store.add(date_str, event)
            if event.get('handoff'):
                # explicit calendar edit with a handoff = re-arm intent
                sync.clear_suppression(store, [event['id']])
            report = await sync.sync_after_change(store=store)
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
            if not store.update_event(date_str, event):
                return {"ok": False, "error": "not found"}
            rearm = sync.rearm_ids(old_events, store.get()["events"])
            if rearm:
                # explicit calendar edit that adds/changes a handoff = re-arm intent
                sync.clear_suppression(store, rearm)
            report = await sync.sync_after_change(store=store)
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
            rearm = sync.rearm_ids(old_events, store.get()["events"])
            if rearm:
                # explicit calendar edit that adds/changes a handoff = re-arm intent
                sync.clear_suppression(store, rearm)
            report = await sync.sync_after_change(store=store)
            # canonical (id-assigned) events let the UI adopt stable ids
            return {"ok": True, "rev": store.get()["rev"], "events": canonical,
                    "sync": report}

        if action == "delete_date":
            store = _get_store()
            if not store.delete_date(input.get("date")):
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        if action == "delete_index":
            store = _get_store()
            date_str = input.get("date")
            index = input.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not store.delete_index(date_str, index)
            ):
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        if action == "delete":
            if not _valid_id(input.get("id")):
                return {"ok": False, "error": "invalid id"}
            store = _get_store()
            if not store.delete_by_id(input.get("id")):
                return {"ok": False, "error": "not found"}
            report = await sync.sync_after_change(store=store)
            return {"ok": True, "rev": store.get()["rev"], "sync": report}

        return {"ok": False, "error": f"unknown action: {action}"}
