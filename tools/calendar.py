"""Mech.Calendar agent tool.

Reads (list/agenda/search) are always allowed. Writes create agent-origin
events which never auto-arm handoffs; updates preserve existing id/origin.
"""
import json
from datetime import date, timedelta

from helpers.tool import Tool, Response

from usr.plugins.mech_calendar.helpers.store import (
    EventStore, sanitize_event, valid_date, _valid_id,
)
from usr.plugins.mech_calendar.helpers import sync

_store: EventStore | None = None


def _get_store() -> EventStore:
    global _store
    if _store is None:
        _store = EventStore()
    return _store


def _fmt(date_str, day_events):
    lines = []
    for ev in day_events:
        t = ev.get("time") or "all-day"
        if ev.get("until"):
            t += "-" + ev["until"]
        desc = (" - " + ev["desc"]) if ev.get("desc") else ""
        lines.append("  {} [{}] {}{}".format(t, ev.get("category", "default"), ev["title"], desc))
    return date_str + "\n" + "\n".join(lines)


def _daterange(from_s, to_s):
    try:
        d0 = date.fromisoformat(from_s)
        d1 = date.fromisoformat(to_s)
    except (TypeError, ValueError):
        return None
    if d1 < d0:
        return None
    days = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 += timedelta(days=1)
    return days


class CalendarTool(Tool):

    async def execute(self, **kwargs) -> Response:
        action = kwargs.get("action")

        if not action:
            return Response(message="Error: action required (list/agenda/search/add/update/delete)", break_loop=False)

        store = _get_store()
        events = store.get()["events"]

        if action == "list":
            if not events:
                return Response(message="Calendar is empty.", break_loop=False)
            out = "\n".join(_fmt(d, evs) for d, evs in sorted(events.items()))
            return Response(message=out, break_loop=False)

        if action == "agenda":
            days = _daterange(kwargs.get("from"), kwargs.get("to"))
            if days is None:
                days = _daterange(date.today().isoformat(), (date.today() + timedelta(days=7)).isoformat())
            out = "\n".join(_fmt(d, events[d]) for d in days if d in events) or "No events in range."
            return Response(message=out, break_loop=False)

        if action == "search":
            q = (kwargs.get("query") or "").lower()
            if not q:
                return Response(message="Error: query required", break_loop=False)
            hits = []
            for d in sorted(events):
                for ev in events[d]:
                    blob = (ev.get("title", "") + " " + ev.get("desc", "") + " " + ev.get("category", "")).lower()
                    if q in blob:
                        hits.append(_fmt(d, [ev]))
            if not hits:
                return Response(message="No events match '" + q + "'.", break_loop=False)
            return Response(message="\n".join(hits), break_loop=False)

        if action == "add":
            date_str = kwargs.get("date")
            if not valid_date(date_str):
                return Response(message="Error: invalid date " + repr(date_str), break_loop=False)
            raw = {
                "title": kwargs.get("title"),
                "time": kwargs.get("time", ""),
                "until": kwargs.get("until", ""),
                "category": kwargs.get("category", "default"),
                "desc": kwargs.get("desc", ""),
                "origin": "agent",
            }
            if kwargs.get("handoff"):
                raw["handoff"] = kwargs.get("handoff")
            event, err = sanitize_event(raw, strict_time=True)
            if err:
                return Response(message="Error: " + err, break_loop=False)
            index = store.add(date_str, event)
            report = await sync.sync_after_change(store=store)
            msg = "OK: added event {} (id {}, index {}) on {}".format(event["title"], event["id"], index, date_str)
            if raw.get("handoff"):
                msg += ". Handoff not armed: agent-created events cannot arm handoffs; the user must confirm it in the calendar UI."
            return Response(message=msg, break_loop=False)

        if action == "update":
            eid = kwargs.get("id")
            if not _valid_id(eid):
                return Response(message="Error: invalid id", break_loop=False)
            date_str = kwargs.get("date")
            if not valid_date(date_str):
                return Response(message="Error: invalid date " + repr(date_str), break_loop=False)
            found = None
            for d, day in events.items():
                for ev in day:
                    if ev.get("id") == eid:
                        found = ev
                        break
            if found is None:
                return Response(message="Error: event not found", break_loop=False)
            merged = dict(found)
            for key in ("title", "time", "until", "category", "desc"):
                if key in kwargs:
                    merged[key] = kwargs[key]
            event, err = sanitize_event(merged, strict_time=True)
            if err:
                return Response(message="Error: " + err, break_loop=False)
            if not store.update_event(date_str, event):
                return Response(message="Error: update failed", break_loop=False)
            report = await sync.sync_after_change(store=store)
            return Response(message="OK: updated event {} -> {} on {}".format(event["title"], eid, date_str), break_loop=False)

        if action == "delete":
            eid = kwargs.get("id")
            if not _valid_id(eid):
                return Response(message="Error: invalid id", break_loop=False)
            if not store.delete_by_id(eid):
                return Response(message="Error: event not found", break_loop=False)
            report = await sync.sync_after_change(store=store)
            return Response(message="OK: deleted event " + eid, break_loop=False)

        return Response(message="Error: unknown action " + repr(action), break_loop=False)
