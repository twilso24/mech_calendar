"""Mech.Calendar lifecycle hooks.

install(): framework calls this after the plugin is placed in usr/plugins/;
arms agent handoffs for existing events via the sync engine.
uninstall(): framework calls this before deleting the plugin directory;
removes every mech_calendar/* scheduled task (no leftover state).
"""
import asyncio
import os

from usr.plugins.mech_calendar.helpers import sync


def _store():
    from usr.plugins.mech_calendar.helpers.store import EventStore
    return EventStore()


def _scheduler():
    from helpers.task_scheduler import TaskScheduler
    return TaskScheduler.get()


def _reconcile(store, scheduler=None):
    return sync.reconcile(store, scheduler=scheduler)


def install(scheduler=None):
    """Arm handoffs for existing events. Never breaks plugin activation."""
    try:
        store = _store()
        sched = scheduler or _scheduler()
        coro = _reconcile(store, scheduler=sched)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop:
            loop.create_task(coro)
        else:
            asyncio.run(coro)
    except Exception as exc:
        print(f"[mech_calendar] install: handoff sync skipped ({exc})")


async def uninstall():
    """Remove all plugin-owned tasks before plugin deletion."""
    try:
        sched = _scheduler()
        for task in list(sched.find_task_by_name(sync.TASK_PREFIX)):
            await sched.remove_task_by_name(task.name)
            sync._purge_chat(getattr(task, 'context_id', None))
    except Exception as exc:
        print(f"[mech_calendar] uninstall: task cleanup skipped ({exc})")
    try:
        ledger = sync._ledger_path(_store())
        if os.path.exists(ledger):
            os.remove(ledger)
    except OSError as exc:
        print(f"[mech_calendar] uninstall: ledger cleanup skipped ({exc})")
