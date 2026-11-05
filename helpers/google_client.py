"""Google Calendar REST client wrapper: injectable service, backoff, error text.

The googleapiclient service is injected so tests never touch the network.
The SDK is imported lazily only when no service is supplied.
"""
from __future__ import annotations

import random
import time

RETRY_STATUSES = (429, 500, 503)
RETRY_REASONS = ('rateLimitExceeded', 'userRateLimitExceeded')
MAX_ATTEMPTS = 4
BASE_DELAY = 1.0


def _status(exc):
    resp = getattr(exc, 'resp', None)
    return getattr(resp, 'status', None)


def _retryable(exc):
    status = _status(exc)
    if status in RETRY_STATUSES:
        return True
    if status == 403:
        body = getattr(exc, 'content', b'') or b''
        if isinstance(body, bytes):
            body = body.decode('utf-8', 'replace')
        return any(reason in str(body) for reason in RETRY_REASONS)
    return False


def build_service(credentials):
    """Create a real googleapiclient service. Imported lazily."""
    from googleapiclient.discovery import build
    return build('calendar', 'v3', credentials=credentials, cache_discovery=False)


class GoogleCalendarClient:
    def __init__(self, service=None, credentials=None):
        self.service = service if service is not None else build_service(credentials)
        self.last_error = ''

    def _run(self, fn):
        """Execute a request with bounded retries and jittered exponential backoff."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                return fn()
            except Exception as exc:
                self.last_error = f'{_status(exc) or "error"}: {exc}'
                if not _retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)

    def list_calendars(self):
        result = self._run(lambda: self.service.calendarList().list().execute())
        return result.get('items', [])

    def list_events(self, calendar_id, sync_token=None, time_min=None, time_max=None):
        kwargs = {'calendarId': calendar_id, 'singleEvents': False, 'showDeleted': True}
        if sync_token:
            kwargs['syncToken'] = sync_token
        if time_min:
            kwargs['timeMin'] = time_min
        if time_max:
            kwargs['timeMax'] = time_max

        items = []
        next_page = None
        next_sync = None
        while True:
            page_kwargs = dict(kwargs)
            if next_page:
                page_kwargs['pageToken'] = next_page
            result = self._run(
                lambda: self.service.events().list(**page_kwargs).execute())
            items.extend(result.get('items', []) or [])
            next_page = result.get('nextPageToken')
            next_sync = result.get('nextSyncToken') or next_sync
            if not next_page:
                break
        return {'items': items, 'nextSyncToken': next_sync}

    def insert_event(self, calendar_id, body):
        return self._run(
            lambda: self.service.events().insert(calendarId=calendar_id, body=body).execute())

    def patch_event(self, calendar_id, event_id, body, etag=None):
        headers = {'If-Match': etag} if etag else {}
        return self._run(lambda: self.service.events().patch(
            calendarId=calendar_id, eventId=event_id, body=body,
            headers=headers).execute())

    def delete_event(self, calendar_id, event_id):
        return self._run(lambda: self.service.events().delete(
            calendarId=calendar_id, eventId=event_id).execute())
