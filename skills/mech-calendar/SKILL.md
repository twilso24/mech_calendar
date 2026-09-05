---
name: mech-calendar
description: Manage the user's Mech.Calendar - list, agenda, search, add, update, and delete events; set up agent handoffs that fire at event start/finish times. Use for "what's on my calendar", "what's Friday look like", "remind me when", scheduling requests, and event-driven automation.
---

# Mech.Calendar

The user's local calendar. Events have a date (YYYY-MM-DD), optional 24h times, categories, and optional agent handoffs. Use the `calendar` tool for all operations.

## Reading the calendar

```json
{"tool_name": "calendar", "tool_args": {"action": "list"}}
{"tool_name": "calendar", "tool_args": {"action": "agenda", "from": "2026-10-01", "to": "2026-10-31"}}
{"tool_name": "calendar", "tool_args": {"action": "search", "query": "dentist"}}
```

- `list` — everything, grouped by date
- `agenda` — windowed range; defaults to next 7 days
- `search` — title/desc/category substring match

## Creating events

```json
{"tool_name": "calendar", "tool_args": {"action": "add", "date": "2026-10-02", "title": "Fee payment deadline", "time": "", "category": "finance", "desc": "$75 per student via Aspen portal"}}
```

- `time` empty = all-day; `until` optional end time; categories: default, work, personal, urgent, social, health, finance.
- Confirm what you added (title, date, time, id).

## Updating / deleting

Id-addressed — fetch the id first:

```json
{"tool_name": "calendar", "tool_args": {"action": "search", "query": "fee"}}
{"tool_name": "calendar", "tool_args": {"action": "update", "id": "evt_0123456789ab", "date": "2026-10-03", "time": "14:00"}}
{"tool_name": "calendar", "tool_args": {"action": "delete", "id": "evt_0123456789ab"}}
```

Omitted fields keep their current values; provide `date` when moving an event.

## Agent handoffs (event-driven automation)

A handoff runs an agent prompt at the event's start and/or finish time. Because agents create events with `origin: agent`, a handoff on an agent-created event **never auto-arms** — get explicit user approval first, then include it:

```json
{"tool_name": "calendar", "tool_args": {"action": "add", "date": "2026-10-02", "title": "Fee deadline", "handoff": {"at": "finish", "prompt": "Check whether the fee was paid and report to the user", "lead_minutes": 30}}}
```

- `at`: start | finish | both. `lead_minutes`: fire earlier (e.g. reminder).
- All-day events fire at 08:00 by default; `finish` uses `until`, else `time`.
- Suggest handoffs when the user says "remind me when...", "check on X at Y", "run X after Z".

## Notes

- The GUI lives at `/plugins/mech_calendar/webui/calendar.html` in the WebUI.
- Handoff tasks appear in the Tasks panel as `mech_calendar/evt_.../start|finish`.
- Deleting or editing an event automatically re-syncs its tasks.
