'''Google sync poller: 10-minute interval, no overlap, skip when disconnected.

The token loader and sync runner are injected so tests stay offline. The
background loop is started by the hook layer, not at import time.
'''
from __future__ import annotations

import threading

POLL_INTERVAL_SECONDS = 600


class PollerState:
    '''Prevents overlapping sync runs.'''

    def __init__(self):
        self._lock = threading.Lock()
        self._busy = False

    def try_enter(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def exit(self) -> None:
        with self._lock:
            self._busy = False


_STATE = PollerState()


def is_paused(path=None):
    """Return True when a pause file exists at path."""
    import os
    path = path or os.environ.get('MECH_CALENDAR_POLLER_PAUSE', '/tmp/mech_calendar_poller.pause')
    return os.path.exists(path)


def poll_once(token_loader, sync_runner, calendar_id='primary', state=None):
    '''Run one poll. Returns ok, not_connected, skipped_overlap, or error: <msg>.'''
    state = state or _STATE
    if not state.try_enter():
        return 'skipped_overlap'
    try:
        token = token_loader()
        if not token or not token.get('refresh_token'):
            return 'not_connected'
        sync_runner(calendar_id)
        return 'ok'
    except Exception as exc:
        return 'error: ' + str(exc)
    finally:
        state.exit()


def poll_calendars(token_loader, calendar_ids, sync_runner, state=None):
    '''Run one non-overlapping poll for every selected calendar.'''
    state = state or _STATE
    if not state.try_enter():
        return 'skipped_overlap'
    try:
        token = token_loader()
        if not token or not token.get('refresh_token'):
            return 'not_connected'
        for calendar_id in calendar_ids():
            sync_runner(calendar_id)
        return 'ok'
    except Exception as exc:
        try:
            from google.auth.exceptions import RefreshError
            if isinstance(exc, RefreshError):
                return 'error: reconnect required'
        except ImportError:
            pass
        return 'error: ' + str(exc)
    finally:
        state.exit()
