"""Mech.Calendar lifecycle hooks.

install(): framework calls this after the plugin is placed in usr/plugins/;
arms agent handoffs for existing events via the sync engine.
uninstall(): framework calls this before deleting the plugin directory;
removes every mech_calendar/* scheduled task (no leftover state).
"""
import asyncio
import os
import threading
import time

from usr.plugins.mech_calendar.helpers import sync
from usr.plugins.mech_calendar.helpers import google_auth
from usr.plugins.mech_calendar.helpers import google_poller



def _store():
    from usr.plugins.mech_calendar.helpers.store import EventStore
    return EventStore()


def _scheduler():
    from helpers.task_scheduler import TaskScheduler
    return TaskScheduler.get()


def _reconcile(store, scheduler=None):
    return sync.reconcile(store, scheduler=scheduler)


_poll_thread = None
_poll_stop = threading.Event()


def _run_poller():
    """Background loop: poll Google every POLL_INTERVAL_SECONDS when connected."""
    while not _poll_stop.is_set():
        try:
            from usr.plugins.mech_calendar.api.events import _google_token
            from usr.plugins.mech_calendar.helpers import google_sync as gsync
            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_poller import poll_calendars
            from usr.plugins.mech_calendar.api.google_sync import _make_client, _sync_calendar_ids, _calendar_metadata
            from usr.plugins.mech_calendar.helpers.store import EventStore

            token = _google_token()
            if token and token.get('refresh_token') and not google_poller.is_paused():
                client = _make_client(token)
                calendar_metadata = _calendar_metadata(client)
                def _sync(calendar_id):
                    store = EventStore()
                    mapping = MappingStore()
                    engine = gsync.GoogleSyncEngine(
                        store=store, client=client, mapping=mapping,
                        calendar_metadata=calendar_metadata)
                    engine.import_calendar(calendar_id)
                    # Reconcile handoffs after every background import so
                    # locally set Google-origin handoffs arm, edits update,
                    # and remote deletions remove stale tasks. sync_after_change
                    # never raises into the poller.
                    try:
                        asyncio.run(sync.sync_after_change(store=store))
                    except RuntimeError:
                        # Running loop in this thread: schedule instead of
                        # recursing with asyncio.run.
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = None
                        if loop is not None:
                            loop.create_task(
                                sync.sync_after_change(store=store))
                        else:
                            raise
                def _calendar_ids():
                    stored = token.get('calendar_id')
                    if stored:
                        return [stored]
                    return _sync_calendar_ids(
                        client, calendar_metadata=calendar_metadata)

                poll_calendars(
                    token_loader=lambda: token,
                    calendar_ids=_calendar_ids,
                    sync_runner=_sync,
                )
        except Exception as exc:
            print(f"[mech_calendar] poller: {exc}")
        _poll_stop.wait(google_poller.POLL_INTERVAL_SECONDS)


def install(scheduler=None):
    """Arm handoffs for existing events. Never breaks plugin activation."""
    global _poll_thread
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
    try:
        # 10-minute Google sync poller; it no-ops until a token exists
        _poll_stop.clear()
        if _poll_thread is None or not _poll_thread.is_alive():
            _poll_thread = threading.Thread(
                target=_run_poller, name='mech_calendar_poller', daemon=True)
            _poll_thread.start()
    except Exception as exc:
        print(f"[mech_calendar] install: poller start skipped ({exc})")


async def uninstall():
    """Remove all plugin-owned tasks before plugin deletion."""
    _poll_stop.set()
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
