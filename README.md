# Mech.Calendar

A local month/week calendar for Agent Zero — multiple calendars with colors and visibility, user-defined labels, Google Calendar two-way sync, export/import, keyboard-first navigation, agent handoffs that run agent tasks at event start/finish times, and a JSON event store served by a plugin-native API.

## Features

- Month and week views with keyboard navigation
- Calendar registry: name, color, visibility, writable flag, source — per-browser visibility in localStorage only
- User-defined labels (no fixed presets) — add/rename/delete with color; events keep the internal `category` field for API/Google-sync compatibility
- All-day strip above the day grid; event markers in day cells use the calendar color; all-day events appear in both
- Event editor calendar picker: only writable calendars offered; read-only calendars show no edit/delete
- Google Calendar two-way sync (discovered user-named calendars; default sync set is primary + detected Holidays unless a calendar is chosen, incremental sync tokens, import + push with If-Match conflicts, 10-minute poller, RRULE occurrence expansion, loopback-only OAuth callback on 127.0.0.1)
- Agent handoffs: UI and Google-origin events with an explicit handoff arm scheduled tasks (agent-origin events never auto-arm)
- Client-side JSON export/import
- All-day (`time` empty) and timed events, strict 24h `HH:MM` validation
- Server-side JSON store (schema v6, cross-instance locking, reload-before-mutation, rev-guarded bulk writes) with atomic writes — data survives restarts
- Runs inside Agent Zero: auth + CSRF handled by the framework
- Marketplace installs provision the Google OAuth/API dependencies automatically before lifecycle startup.

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
| `list` | `{}` | `{ok, rev, events, calendars, labels}` |
| `add` / `update` | `{date, event}` | `{ok, rev, date, id, sync}` |
| `replace` | `{events, base_rev}` | `{ok, rev, events, sync}`; stale `base_rev` → `{ok: false, error: "stale"}` |
| `delete` / `delete_index` / `delete_date` | `{id}` / `{date, index}` / `{date}` | `{ok, rev, sync}` |
| `calendars` / `calendars_save` / `calendar_save` | `{}` / `{calendars, base_rev}` | calendar registry (server-owned `writable`/`source`/`google_calendar_id` preserved) |
| `labels_save` | `{labels, base_rev}` | user label registry; renames propagate to events, deletes unlabel events |
| `google_sync` | `{action: status|connect|callback|disconnect|sync|list_calendars|set_calendar}` | Google Calendar sync; tokens never returned |

Event limits: `title` ≤ 200 chars (required), `desc` ≤ 2000, `time` optional strict 24h on `add`/`update` / normalized on `replace`, `category` is a user label name (free-form, ≤50 chars; empty = unlabeled; legacy `default` migrates to unlabeled). Google-origin events keep `google_id` and `origin: google`.

## Data & Privacy

Events are stored locally at `usr/plugins/mech_calendar/data/events.json` (format `{version: 6, rev: <n>, calendars: {...}, labels: [...], events: {date: [event, ...]}}`; `rev` bumps on every mutation and guards stale writers; Google sync mapping lives alongside it). The `data/` directory is gitignored — **no calendar data is ever shipped with the plugin**. Uninstalling removes the plugin; export your data first if you want to keep it. Google OAuth tokens are stored outside the repo by `helpers/google_auth.py`.

## Roadmap

- v1.3.0 — ✅ shipped: label CRUD + collapsible all-day strip (see Changelog)
- v1.4 — iCal import/export, sharing

## Changelog

### 1.3.0
- Label CRUD: explicit `label_create` / `label_update` / `label_delete` API actions with `base_rev` optimistic locking, plus a label-management modal (create / edit / delete with confirmation) in the event editor; renames propagate to events, deletes unlabel events (`labels_save` retained for compatibility)
- Collapsible all-day strip above the grid with event count and persisted collapsed state; strip capped at three rows with horizontal scrolling
- Marketplace install hardening: missing Google OAuth/API packages are provisioned in the active framework runtime with bounded installation and import validation.
- Calendar registry (schema v5→v6): name, color, visibility, writable, source, `google_calendar_id`; sidebar list with color swatches, source, counts, and per-browser visibility (localStorage only, reapplied after async server load)
- User-defined labels replace the seven fixed category presets (fresh stores start empty; v5 values migrate to labels; legacy `default` migrates to unlabeled; internal field stays `category`)
- All-day strip above the grid + all-day markers in day cells, so strip and grid always agree; event markers use calendar color
- Event editor calendar picker (writable calendars only); read-only calendars show no edit/delete; unknown calendar IDs rejected
- Google Calendar two-way sync: discovered user-named calendars (default sync set is primary + detected Holidays unless a calendar is chosen), incremental sync tokens with 410 full-resync, push with If-Match (Google wins conflicts), 10-minute poller, RRULE occurrence expansion, loopback-only OAuth callback (`127.0.0.1`), explicit calendar validation, sanitized sync errors
- Handoffs now arm for Google-origin events with an explicit UI handoff (agent-origin stays excluded); Google re-import preserves local handoffs; manual sync and background poller reconcile handoffs after every import
- Store hardening: cross-instance file locking, reload-before-mutation, rev-guarded bulk calendar/label saves, defensive handoff parsing, case-insensitive label uniqueness
- Suite: 372 passing (project) / plugin contract tests green

### 1.1.8
- Mobile fix: the event edit sheet is drag-scrollable again — the event drawer's touch scroll-lock no longer cancels drags inside the sheet or the week grid (regression introduced by day-tap auto-open; BUILD 20260905A)
- Regression tests for the touch-guard whitelist order (suite now 136 tests)

### 1.1.7
- Floating windows ("Open as window") now fill their modal and grow when resized — scoped flex chain in the panel wrapper with a 320px min-height floor (docked canvas unaffected)
- Versioned internal asset URLs (`?v=20260904c`) so service-worker caches can't serve stale panels
- Mobile usability pass (BUILD 20260904A/C)

### 1.1.6
- Disarming an agent handoff (DISARM button, or unchecking AGENT HANDOFF on an existing event) now asks for confirmation before removing the handoff and its scheduled task

## License

Apache License 2.0 — see [LICENSE](LICENSE).
