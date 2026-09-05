# Mech.Calendar

A local month/week calendar for Agent Zero — categories, export/import, keyboard-first navigation, and a JSON event store served by a plugin-native API.

## Features

- Month, week, and all-events views with keyboard navigation
- Seven event categories (default/work/personal/urgent/social/health/finance)
- Client-side JSON export/import
- All-day (`time` empty) and timed events, strict 24h `HH:MM` validation
- Server-side JSON store with atomic writes — data survives restarts
- Runs inside Agent Zero: auth + CSRF handled by the framework

## Open

From the Agent Zero WebUI (logged in):

```
/plugins/mech_calendar/webui/calendar.html
```

## API

Single endpoint, action-based:

```
POST /api/plugins/mech_calendar/events
X-CSRF-Token: <token from GET /api/csrf_token>
```

| Action | Input | Returns |
|---|---|---|
| `list` | `{}` | `{ok, events}` |
| `add` | `{date, event}` | `{ok, date, index}` |
| `replace` | `{events}` | `{ok}` (bulk, sanitized) |
| `delete_date` | `{date}` | `{ok}` |
| `delete_index` | `{date, index}` | `{ok}` |

Event limits: `title` ≤ 200 chars (required), `desc` ≤ 2000, `time` optional strict 24h on `add` / normalized on `replace`, `category` from the fixed set (unknown → `default`).

## Data & Privacy

Events are stored locally at `usr/plugins/mech_calendar/data/events.json` (format `{version: 3, rev: <n>, events: {date: [event, ...]}}`; `rev` bumps on every mutation and guards stale writers). The `data/` directory is gitignored — **no calendar data is ever shipped with the plugin**. Uninstalling removes the plugin; export your data first if you want to keep it.

## Roadmap

- v1.1.5 — ✅ shipped (found via live-fire E2E): fired-task guards — after a handoff fires, calendar edits within the grace window no longer rewrite its plan (which would have made the task instantly due and refire forever); removing a fired task now PRESERVES its dedicated chat (the transcript is the handoff's output); time-edits of fired handoffs re-arm cleanly under the same identity. Backend-only (no UI change).
- v1.1.4 — ✅ shipped: stable task identity — every handoff now owns ONE permanent task + chat identity (`mcal_<hash>` derived from event id + kind). Edits update the task in place (same uuid, same chat, same sidebar row) instead of delete+re-add. Fixes: ghost-chat spam (each toggle/edit used to mint a new dedicated chat; 11 purged), "Task not found" in the Tasks panel (uuid churn), and task deletions/disarms now clean up the dedicated chat like the framework's own delete API. Legacy duplicate identities are deduped; ≤v1.1.3 armed-ledger keys migrate transparently; `updated` count surfaces in the save toast.
- v1.1.3 — ✅ shipped: automation workflow fix — store revision guard (`rev` + `base_rev` on `replace`) ends the stale-surface wipe loop that erased handoffs (a conflicting calendar surface now reloads instead of overwriting); past-dated handoffs are reported via `skipped_stale` + toast instead of being silently skipped; automation proven live (fee-deadline handoff armed through the real scheduler)
- v1.1.2 — ✅ shipped: re-arm intent — calendar edits that add or change a handoff clear the deletion tombstone and re-arm the task (previously, after deleting a task in the Tasks panel, re-saving the same handoff could never re-arm it). UI: empty-prompt guard (handoff needs the agent prompt) and id-resolved edit merge (async state adoption can no longer retarget or silently drop an edit).
- v1.1.1 — ✅ shipped: handoff sync respects user deletions — deleting a `mech_calendar/*` task in the Tasks panel now sticks (armed-ledger with content fingerprints; previously the next calendar change resurrected it). Handoff prompt edits also propagate to already-armed tasks.
- v1.1 — ✅ shipped: stable event ids, scheduler handoff (events arm agent tasks at start/finish times, editable in the GUI via the AGENT HANDOFF section), `calendar` agent tool + bundled `mech-calendar` skill
- v1.2 — native A0 UI integration (Alpine store, toasts, sidebar entry, settings)
- v1.3 — multi-calendar, iCal import/export, sharing

## Changelog

### 1.1.7
- Floating windows ("Open as window") now fill their modal and grow when resized — scoped flex chain in the panel wrapper with a 320px min-height floor (docked canvas unaffected)
- Versioned internal asset URLs (`?v=20260904c`) so service-worker caches can't serve stale panels
- Mobile usability pass (BUILD 20260904A/C)

### 1.1.6
- Disarming an agent handoff (DISARM button, or unchecking AGENT HANDOFF on an existing event) now asks for confirmation before removing the handoff and its scheduled task

## License

Apache License 2.0 — see [LICENSE](LICENSE).
