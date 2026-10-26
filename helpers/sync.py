"""Mech.Calendar handoff sync engine.

Arms agent work at event boundaries: each ui-origin event with a handoff
desired one PlannedTask (name mech_calendar/evt_<id>/<start|finish>) in the
framework TaskScheduler. The framework JobLoop fires due tasks; this module
only reconciles desired vs existing state after calendar mutations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from usr.plugins.mech_calendar.helpers.store import EventStore

TASK_PREFIX = 'mech_calendar/'
ALLDAY_DEFAULT = timedelta(hours=8)  # all-day events fire at 08:00
DEFAULT_GRACE_MINUTES = 60


def fire_times(date_str, event):
    """[(kind, datetime)] a handoff should fire for this event, unguarded."""
    handoff = event.get('handoff') if isinstance(event, dict) else None
    if not isinstance(handoff, dict):
        return []
    y, m, d = (int(p) for p in date_str.split('-'))

    def at(kind):
        if kind == 'start':
            hh, mm = _parse_hm(event.get('time')) or (8, 0)
        else:  # finish: until, else time, else all-day default
            hh, mm = (_parse_hm(event.get('until'))
                      or _parse_hm(event.get('time'))
                      or (8, 0))
        base = datetime(y, m, d, hh, mm)
        lead = int(handoff.get('lead_minutes') or 0)
        when = base - timedelta(minutes=lead)
        day_start = datetime(y, m, d)
        if when < day_start:
            when = day_start  # lead crossing midnight clamps
        return when

    order = ('start', 'finish') if handoff.get('at') == 'both' else (handoff.get('at'),)
    return [(kind, at(kind)) for kind in order]


def _parse_hm(value):
    if isinstance(value, str) and len(value) == 5 and value[2] == ':':
        try:
            hh, mm = int(value[:2]), int(value[3:])
        except ValueError:
            return None
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    return None


def should_arm(when, now=None, grace_minutes=DEFAULT_GRACE_MINUTES):
    """Skip handoffs already past (post-downtime catch-up spam guard)."""
    if when is None:
        return False
    if now is None:
        now = datetime.now()
    return now <= when or (when - now).total_seconds() >= -grace_minutes * 60


def task_name(event, kind):
    eid = event.get('id') or 'unknown'
    title = str(event.get('title') or 'untitled').strip()[:60] or 'untitled'
    return f'{TASK_PREFIX}{title} ({kind}) [{eid}]'


def desired_tasks(store, now=None):
    """[(name, when, event)] every handoff that should currently be armed."""
    if now is None:
        now = datetime.now()
    wanted = []
    for date_str, day in store.get()['events'].items():
        for event in day:
            if event.get('origin') != 'ui':
                continue  # agent-authored events never auto-arm
            for kind, when in fire_times(date_str, event):
                if should_arm(when, now=now):
                    ev = dict(event, _date=date_str, _kind=kind)
                    wanted.append((task_name(event, kind), when, ev))
    return wanted


def _prompt_for(event, kind):
    handoff = event.get('handoff') or {}
    user_prompt = str(handoff.get('prompt') or '').strip()
    lines = [
        f'Calendar handoff ({kind}) for event "{event.get("title", "untitled")}".',
        f'Date: {event.get("_date", "")} Time: {event.get("time") or "all day"}'
        + (f'-{event["until"]}' if event.get('until') else ''),
        f'Category: {event.get("category", "default")}',
    ]
    if event.get('desc'):
        lines.append(f'Description: {event["desc"]}')
    lines.append('')
    lines.append('Your task from the user:')
    lines.append(user_prompt or 'Remind the user about this event.')
    lines.append('')
    lines.append('Rules: use the calendar tool to read events if needed. Do NOT create')
    lines.append('new calendar events or scheduled tasks. Report back to the user.')
    return '\n'.join(lines)


SYSTEM_PROMPT = (
    'You are handling a Mech.Calendar event handoff. Follow the task text.')


def _build_task(name, when, event, project_name=None):
    from helpers.task_scheduler import PlannedTask, TaskPlan
    kind = event.get('_kind') or 'start'
    # deterministic uuid == dedicated context id: one chat identity per
    # handoff across arm/edit/re-arm cycles (framework treats uuid as the
    # task-chat identity, so uuid must stay stable, not just context_id)
    return PlannedTask(
        uuid=context_id_for(event.get('id'), kind),
        name=name,
        system_prompt=SYSTEM_PROMPT,
        prompt=_prompt_for(event, kind),
        plan=TaskPlan.create(todo=[when]),
        project_name=project_name,
    )


def _plan_for(when):
    from helpers.task_scheduler import TaskPlan
    return TaskPlan.create(todo=[when])


def _existing_map(scheduler):
    """(stable_key -> task, duplicates) for plugin-owned tasks.

    Tasks whose name carries no parsable identity (legacy or foreign
    formats) are keyed by raw name so they still reconcile as orphans.
    Legacy churn can leave several live tasks claiming one identity
    (same event id + kind); the first is kept, the rest return as
    duplicates for removal.
    """
    existing = {}
    duplicates = []
    for task in scheduler.find_task_by_name(TASK_PREFIX):
        key = _stable_key_from_name(task.name) or task.name
        if key in existing:
            duplicates.append(task)
        else:
            existing[key] = task
    return existing, duplicates


def _same_time(a, b):
    return [x.replace(tzinfo=None) for x in a] == [x.replace(tzinfo=None) for x in b]


def _fired_at(task):
    """Fire time this task already executed, or None.

    A fired planned task has empty plan.todo, its fire time moved to
    plan.done, and last_run set (also true for errored runs).
    """
    if getattr(task, 'last_run', None) is None:
        return None
    done = list(getattr(task.plan, 'done', []) or [])
    todos = list(getattr(task.plan, 'todo', []) or [])
    if len(done) == 1 and not todos:
        return done[0].replace(tzinfo=None)
    return None


def _ledger_path(store):
    return re.sub(r'\.json$', '.armed.json', store.path)


def stable_key(event_id, kind):
    """Stable identity for one handoff (event id + fire kind)."""
    return f'{event_id}/{kind}'


def context_id_for(event_id, kind):
    """Deterministic dedicated-chat id so re-arms reuse the same chat
    instead of minting a fresh context per task uuid."""
    digest = hashlib.sha1(f'{event_id}|{kind}'.encode()).hexdigest()[:10]
    return f'mcal_{digest}'


_NAME_TAIL_RE = re.compile(r'\[([A-Za-z0-9_]+)\]\s*$')
_NAME_KIND_RE = re.compile(r'\((start|finish)\)\s*\[')


def _stable_key_from_name(name):
    """Recover (event_id, kind) key from a task name, else None."""
    m_tail = _NAME_TAIL_RE.search(name or '')
    m_kind = _NAME_KIND_RE.search(name or '')
    if not (m_tail and m_kind):
        return None
    return stable_key(m_tail.group(1), m_kind.group(1))


def _purge_chat(context_id):
    """Remove a task's dedicated chat (framework task-delete contract)."""
    if not context_id:
        return
    try:
        from agent import AgentContext
        from helpers import persist_chat
        AgentContext.remove(context_id)
        persist_chat.remove_chat(context_id)
    except Exception:
        pass  # cleanup must never break sync


def _fingerprint(when, event):
    """Content fingerprint of an armed handoff: fire time + rendered prompt."""
    raw = f'{when.isoformat()}|{_prompt_for(event, event.get("_kind") or "start")}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _load_ledger(store):
    """Load armed-ledger, migrating <=v1.1.3 task-name keys to stable keys."""
    try:
        with open(_ledger_path(store), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    migrated = {}
    for key, val in data.items():
        if not isinstance(key, str):
            continue
        stable = _stable_key_from_name(key)
        if stable:
            migrated.setdefault(stable, val)  # old task-name key -> stable key
        else:
            migrated.setdefault(key, val)
    return migrated


def _save_ledger(store, ledger):
    try:
        tmp = _ledger_path(store) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(ledger, fh, indent=1, sort_keys=True)
        os.replace(tmp, _ledger_path(store))
    except OSError:
        pass  # the ledger is bookkeeping; sync must never fail on it


def _handoff_sig(event):
    """Comparable handoff signature or None when absent/invalid."""
    h = event.get('handoff') if isinstance(event, dict) else None
    if not isinstance(h, dict):
        return None
    return (h.get('at'), str(h.get('prompt') or '').strip(), int(h.get('lead_minutes') or 0))


def rearm_ids(old_events, new_events):
    """Event ids whose handoff appeared or changed between two stores.

    An explicit calendar edit that adds or modifies a handoff expresses
    re-arm intent, even if the fingerprint matches an old tombstone.
    """
    old = {}
    for day in (old_events or {}).values():
        for e in day:
            if isinstance(e, dict) and e.get('id'):
                old[e['id']] = _handoff_sig(e)
    out = []
    for day in (new_events or {}).values():
        for e in day:
            if not (isinstance(e, dict) and e.get('id')):
                continue
            sig = _handoff_sig(e)
            if sig is not None and old.get(e['id']) != sig:
                out.append(e['id'])
    return out


def clear_suppression(store, ids):
    """Remove deletion tombstones for these event ids (explicit re-arm).

    Ledger keys are task names ending in "[<event_id>]"; only matching
    entries are dropped so unrelated suppressions survive. Never creates
    the ledger file when nothing is tombstoned.
    """
    if not ids:
        return
    ledger = _load_ledger(store)
    if not ledger:
        return
    prefixes = tuple(f'{eid}/' for eid in ids)      # stable keys: evt_x/start
    suffixes = tuple(f'[{eid}]' for eid in ids)     # legacy task-name keys
    changed = False
    for name in list(ledger):
        if name.startswith(prefixes) or name.endswith(suffixes):
            del ledger[name]
            changed = True
    if changed:
        _save_ledger(store, ledger)


def count_stale(store, now=None):
    """Handoff fire-times already past the stale guard.

    These are never armed silently; the number is surfaced in the reconcile
    report so the UI can tell the user why nothing fired.
    """
    if now is None:
        now = datetime.now()
    count = 0
    for date_str, day in store.get()['events'].items():
        for event in day:
            if event.get('origin') != 'ui':
                continue
            for _kind, when in fire_times(date_str, event):
                if not should_arm(when, now=now):
                    count += 1
    return count


async def reconcile(store, scheduler=None, now=None):
    """Make scheduler match desired state, respecting user deletions.

    Returns {'armed', 'removed', 'suppressed'}. The armed-ledger
    (<store>.armed.json) records a fingerprint (fire time + prompt) for
    every task we arm. A desired task that is missing, ledgered, and
    unchanged was deleted by the user (or already fired): suppress it
    instead of resurrecting it. A changed fingerprint is a genuine edit
    and re-arms with the new content.
    """
    if scheduler is None:
        from helpers.task_scheduler import TaskScheduler
        scheduler = TaskScheduler.get()
        await scheduler.reload()

    wanted = desired_tasks(store, now=now)
    # key everything by stable identity (event id + kind), not task name:
    # titles can change; identity must not
    want_meta = {}
    for name, when, event in wanted:
        key = stable_key(event.get('id'), event.get('_kind') or 'start')
        want_meta[key] = (name, when, event)
    want_fp = {key: _fingerprint(when, ev)
               for key, (_n, when, ev) in want_meta.items()}
    existing_by_key, duplicates = _existing_map(scheduler)
    ledger = _load_ledger(store)  # migrates <=v1.1.3 task-name keys
    armed = removed = suppressed = updated = 0
    # legacy churn cleanup: exactly one live task per identity
    for dup in duplicates:
        await scheduler.remove_task_by_uuid(dup.uuid)
        if getattr(dup, 'last_run', None) is None:
            _purge_chat(getattr(dup, 'context_id', None))
        removed += 1

    # handoffs that left the desired set (disarmed, event deleted, stale)
    # drop their ledger entry so an identical handoff can arm again later
    for key in list(ledger):
        if key not in want_meta:
            del ledger[key]

    # reconcile pass over existing plugin tasks
    for key, task in list(existing_by_key.items()):
        never_fired = getattr(task, 'last_run', None) is None
        if key not in want_meta:
            await scheduler.remove_task_by_name(task.name)
            if never_fired:
                # fired tasks keep their chat: the transcript is the
                # handoff's output and belongs to the user
                _purge_chat(getattr(task, 'context_id', None))
            removed += 1
            ledger.pop(key, None)
            continue
        name, when, event = want_meta[key]
        todos = list(getattr(task.plan, 'todo', []) or [])
        fp = ledger.get(key)
        if _fired_at(task) == when.replace(tzinfo=None):
            # already fired at exactly this time; rewriting the plan would
            # make it instantly due and refire forever
            if fp is None:
                ledger[key] = want_fp[key]
            continue
        if fp is None:
            # pre-ledger task: adopt in place if fire time still matches
            if _same_time(todos, [when]) and task.name == name:
                ledger[key] = want_fp[key]
                continue
        elif fp == want_fp[key] and _same_time(todos, [when]) and task.name == name:
            continue
        # genuine edit: update in place so uuid (and its dedicated chat)
        # never churns - the ghost-chat / Task-not-found fix
        await scheduler.update_task(
            task.uuid, name=name,
            prompt=_prompt_for(event, event.get('_kind') or 'start'),
            plan=_plan_for(when))
        updated += 1
        ledger[key] = want_fp[key]

    # arm pass: desired handoffs with no live task
    for key, (name, when, event) in want_meta.items():
        if key in existing_by_key:
            continue
        if ledger.get(key) == want_fp[key]:
            suppressed += 1  # user-deleted (or already fired): respect it
            continue
        await scheduler.add_task(_build_task(name, when, event))
        armed += 1
        ledger[key] = want_fp[key]

    _save_ledger(store, ledger)
    return {'armed': armed, 'removed': removed, 'suppressed': suppressed,
            'updated': updated,
            'skipped_stale': count_stale(store, now=now)}


async def sync_after_change(store=None, scheduler=None):
    """Reconcile after a calendar mutation. Never raises into the API layer."""
    try:
        if store is None:
            from usr.plugins.mech_calendar.api.events import _get_store
            store = _get_store()
        return await reconcile(store, scheduler=scheduler)
    except Exception as exc:
        return {'armed': 0, 'removed': 0, 'error': str(exc)}
