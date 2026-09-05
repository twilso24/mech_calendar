# Tool: calendar - Mech.Calendar event management

Manage the user's local calendar: events with dates, optional times, categories, and agent handoffs.

## Actions

- `list` — all events, grouped by date
- `agenda` — events between `from` and `to` (ISO dates; defaults to next 7 days)
- `search` — events matching `query` in title/desc/category
- `add` — create event (requires `date`, `title`; optional `time`, `until`, `category`, `desc`, `handoff`)
- `update` — edit by event `id` (fields you omit are kept; provide target `date` if it moves)
- `delete` — remove by event `id`

## Rules

- Read actions (`list`, `agenda`, `search`) are always safe — use them freely.
- Dates are `YYYY-MM-DD`; times are 24h `HH:MM`; empty `time` = all-day.
- Categories: `default`, `work`, `personal`, `urgent`, `social`, `health`, `finance`.
- Use `add` when the user asks to schedule something. Keep `desc` concise.
- `update`/`delete` are id-addressed: run `list` or `search` first to get the id, then act.
- Events you create are marked `origin: agent`. They never auto-arm agent handoffs — if a handoff makes sense, ask the user to confirm, then include the `handoff` object: `{"at": "start|finish|both", "prompt": "...", "lead_minutes": N}`.
- After any write, briefly confirm what changed (title, date, time, id).
- When the user asks "what's on my calendar", "what's Friday look like", or "remind me when...", use this tool.

## Example

```json
{"action": "add", "date": "2026-10-02", "title": "Fee payment deadline", "time": "", "category": "finance", "desc": "$75 per student via Aspen portal"}
```
