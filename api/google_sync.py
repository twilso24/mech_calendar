"""Mech.Calendar Google sync API: connect, callback, disconnect, status, sync.

Tokens are never returned. The last sync error is kept in module state so the
status action can show it. The connect flow uses a Desktop-app OAuth loopback
redirect on 127.0.0.1 with an OS-assigned port; no public base URL or WebUI
callback route is required.
"""
import html
import hmac
import http.server
import threading
import time
import urllib.parse

from flask import Request
from helpers.api import ApiHandler, Input, Output

from usr.plugins.mech_calendar.helpers import google_auth as auth
from usr.plugins.mech_calendar.helpers import google_sync as sync_mod
from usr.plugins.mech_calendar.helpers import sync as handoff_sync
from usr.plugins.mech_calendar.helpers.google_map import MappingStore

_CALLBACK_HOST = '127.0.0.1'
_CALLBACK_BIND = '127.0.0.1'
_callback_server = None
_callback_thread = None
_callback_lock = threading.Lock()
_flow_lock = threading.Lock()
_FLOW_TIMEOUT_SECONDS = 600


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the OAuth redirect from Google and completes the flow."""

    def do_GET(self):
        global _oauth_flow, _oauth_state, _oauth_expires_at, _last_error
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != _CALLBACK_PATH:
            self._send_html(404, '<h1>Not found</h1>')
            return

        with _flow_lock:
            flow = _oauth_flow
            expected_state = _oauth_state
            expires_at = _oauth_expires_at

        supplied_state = params.get('state', [None])[0]
        if (flow is None or not expected_state or not supplied_state
                or not hmac.compare_digest(str(supplied_state), str(expected_state))):
            self._send_html(400, '<h1>Invalid authorization response</h1>')
            return

        if expires_at is not None and time.monotonic() >= expires_at:
            _clear_pending_flow()
            self._send_html(400, '<h1>Authorization expired</h1>')
            _stop_callback_async()
            return

        error = params.get('error', [None])[0]
        code = params.get('code', [None])[0]
        if error:
            consumed, expired = _consume_pending_flow(flow, supplied_state)
            if expired:
                self._send_html(400, '<h1>Authorization expired</h1>')
            elif consumed is None:
                self._send_html(400, '<h1>Invalid authorization response</h1>')
            else:
                _last_error = 'authorization denied'
                self._send_html(400, '<h1>Authorization failed</h1>')
            if consumed is not None or expired:
                _stop_callback_async()
            return
        if not code:
            self._send_html(400, '<h1>No authorization code received</h1>')
            return

        flow, expired = _consume_pending_flow(flow, supplied_state)
        if expired:
            self._send_html(400, '<h1>Authorization expired</h1>')
            _stop_callback_async()
            return
        if flow is None:
            self._send_html(400, '<h1>Invalid authorization response</h1>')
            return

        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            token = {
                'access_token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': list(creds.scopes or []),
            }
            auth.save_token(token)
            self._send_html(200, '<h1>Connected!</h1><p>You can close this tab and return to Agent Zero.</p>')
        except Exception as exc:
            _last_error = 'connection failed: ' + type(exc).__name__
            self._send_html(400, '<h1>Connection failed</h1>')
        _stop_callback_async()

    def _send_html(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(f'<html><body>{body}</body></html>'.encode('utf-8'))

    def log_message(self, *args):
        pass  # Suppress request logging


def _start_callback_server():
    """Start the OAuth callback server on localhost. Returns (port, error)."""
    global _callback_server, _callback_thread
    with _callback_lock:
        if _callback_server is not None:
            return _callback_server.server_address[1], None
        try:
            # Port 0: the OS picks a free loopback port, so no fixed port
            # can collide with other local services.
            _callback_server = http.server.HTTPServer(
                (_CALLBACK_BIND, 0), _CallbackHandler)
            _callback_thread = threading.Thread(
                target=_callback_server.serve_forever, daemon=True)
            _callback_thread.start()
            return _callback_server.server_address[1], None
        except OSError as exc:
            _callback_server = None
            _callback_thread = None
            return None, str(exc)


def _stop_callback_server():
    """Stop the callback server. Safe to call when not running."""
    global _callback_server, _callback_thread
    with _callback_lock:
        if _callback_server is not None:
            try:
                _callback_server.shutdown()
            except Exception:
                pass
            _callback_server = None
            _callback_thread = None


def _client_credentials_from_secrets():
    """Load CLIENT_ID and CLIENT_SECRET from the secrets store if available.

    Returns (client_id, client_secret) or (None, None). Never raises.
    """
    try:
        from helpers.secrets import get_default_secrets_manager
        secrets = get_default_secrets_manager().load_secrets()
        cid = secrets.get('CLIENT_ID')
        csec = secrets.get('CLIENT_SECRET')
        if cid and csec:
            return str(cid), str(csec)
    except Exception:
        pass
    return None, None

_last_error = ''
_oauth_flow = None  # in-progress Flow between connect and callback
_oauth_state = None  # state issued with that Flow; verified by the callback route
_oauth_expires_at = None  # monotonic deadline for the pending flow
_OAUTH_SETUP = object()  # reservation while the loopback flow is being created


def _clear_pending_flow():
    """Clear the pending OAuth transaction; safe to call repeatedly."""
    global _oauth_flow, _oauth_state, _oauth_expires_at
    with _flow_lock:
        _oauth_flow = None
        _oauth_state = None
        _oauth_expires_at = None


def _consume_pending_flow(flow, state):
    """Atomically consume a validated pending transaction."""
    global _oauth_flow, _oauth_state, _oauth_expires_at
    with _flow_lock:
        if _oauth_flow is not flow or _oauth_state is None:
            return None, False
        if not hmac.compare_digest(str(_oauth_state), str(state)):
            return None, False
        if (_oauth_expires_at is not None
                and time.monotonic() >= _oauth_expires_at):
            _oauth_flow = None
            _oauth_state = None
            _oauth_expires_at = None
            return None, True
        _oauth_flow = None
        _oauth_state = None
        _oauth_expires_at = None
        return flow, False


def _stop_callback_async():
    """Stop the callback server outside the HTTP server thread."""
    threading.Thread(target=_stop_callback_server, daemon=True).start()

DEFAULT_SCOPES = ['https://www.googleapis.com/auth/calendar']
HOLIDAY_CALENDAR_MARKER = 'en.usa#holiday@group.v.calendar.google.com'


def _calendar_metadata(client):
    """Return discovered metadata keyed by Google calendar ID.

    Discovery is best effort because older OAuth tokens may lack calendar-list
    scope. Missing discovery must not prevent the known sync fallback IDs from
    being imported.
    """
    try:
        calendars = client.list_calendars()
    except Exception:
        return {}
    if not isinstance(calendars, list):
        return {}
    metadata = {}
    for calendar in calendars:
        if not isinstance(calendar, dict) or not calendar.get('id'):
            continue
        calendar_id = str(calendar['id'])
        metadata[calendar_id] = {
            'summary': str(calendar.get('summary') or '').strip(),
            'accessRole': str(calendar.get('accessRole') or '').strip(),
            'primary': bool(calendar.get('primary')),
        }
    return metadata


def _sync_calendar_ids(client, calendar_metadata=None):
    """Return primary and detected Holidays, including scope fallback."""
    metadata = calendar_metadata if calendar_metadata is not None else _calendar_metadata(client)
    if not metadata:
        # Older tokens may have calendar.events but not calendar-list scope.
        # The known Holidays feed can still be read directly with that scope.
        return ['primary', HOLIDAY_CALENDAR_MARKER]

    selected = []
    for calendar_id, details in metadata.items():
        summary = str(details.get('summary') or '').lower()
        is_primary = calendar_id == 'primary' or details.get('primary') is True
        is_holiday = (
            HOLIDAY_CALENDAR_MARKER in calendar_id.lower()
            or 'holiday' in summary
            or 'holidays' in summary
        )
        if (is_primary or is_holiday) and calendar_id not in selected:
            selected.append(calendar_id)

    return selected or ['primary']


def _is_refresh_error(exc):
    """Detect google.auth RefreshError, including wrapped exceptions."""
    try:
        from google.auth.exceptions import RefreshError
    except Exception:
        RefreshError = None
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if RefreshError is not None and isinstance(current, RefreshError):
            return True
        if type(current).__name__ == 'RefreshError':
            return True
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
    return False


def _make_client(token):
    """Build a real Google Calendar client from a stored token. Lazy imports."""
    from google.oauth2.credentials import Credentials
    from usr.plugins.mech_calendar.helpers.google_client import GoogleCalendarClient
    creds = Credentials(
        token=token.get('access_token'),
        refresh_token=token.get('refresh_token'),
        token_uri=token.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=token.get('client_id'),
        client_secret=token.get('client_secret'),
        scopes=token.get('scopes') or None,
    )
    return GoogleCalendarClient(credentials=creds)


def _make_store():
    """Shared event store (same instance the calendar API uses) and sidecar map."""
    from usr.plugins.mech_calendar.api.events import _get_store
    return _get_store(), MappingStore()


def _client_config(client_id, client_secret, redirect_uri):
    return {
        'installed': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [redirect_uri],
        }
    }


_CALLBACK_PATH = '/api/plugins/mech_calendar/google_oauth_callback'
_last_calendar_ids = []






_SAFE_ERROR_LABELS = ('authorization denied', 'connection failed: ', 'sync failed: ')



def _sanitize_error(text):
    """Keep only known safe labels; provider text may hold tokens."""
    text = str(text or '')
    if not text:
        return ''
    if text == 'authorization denied':
        return text
    for label in _SAFE_ERROR_LABELS[1:]:
        if text.startswith(label) and text[len(label):].isidentifier():
            return text
    return 'provider error'


class GoogleSyncApi(ApiHandler):
    async def process(self, input: Input, request: Request) -> Output:
        global _last_error, _oauth_flow, _oauth_state, _oauth_expires_at, _last_calendar_ids
        action = input.get('action')

        if action == 'status':
            token = auth.load_token()
            status = auth.public_status(token)
            cid, csec = _client_credentials_from_secrets()
            return {'ok': True, 'last_error': _sanitize_error(_last_error),
                    'credentials_available': bool(cid and csec),
                    **status}

        if action == 'connect':
            client_id = input.get('client_id')
            client_secret = input.get('client_secret')
            if not client_id or not client_secret:
                client_id, client_secret = _client_credentials_from_secrets()
            if not client_id or not client_secret:
                return {'ok': False, 'error': 'client_id and client_secret required'}

            with _flow_lock:
                if _oauth_flow is not None:
                    return {'ok': False, 'error': 'an OAuth connection flow is already active'}
                _oauth_flow = _OAUTH_SETUP

            try:
                port, _server_error = _start_callback_server()
            except Exception:
                _clear_pending_flow()
                _stop_callback_server()
                return {'ok': False, 'error': 'could not start local callback server'}
            if not port:
                _clear_pending_flow()
                _stop_callback_server()
                return {'ok': False, 'error': 'could not start local callback server'}
            redirect_uri = 'http://%s:%s%s' % (_CALLBACK_HOST, port, _CALLBACK_PATH)
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                scopes = input.get('scopes') or DEFAULT_SCOPES
                flow = InstalledAppFlow.from_client_config(
                    _client_config(client_id, client_secret, redirect_uri), scopes=scopes)
                flow.redirect_uri = redirect_uri
                auth_url, state = flow.authorization_url(
                    access_type='offline', prompt='consent')
                with _flow_lock:
                    _oauth_flow = flow
                    _oauth_state = state
                    _oauth_expires_at = time.monotonic() + _FLOW_TIMEOUT_SECONDS
            except Exception:
                _clear_pending_flow()
                _stop_callback_server()
                return {'ok': False, 'error': 'could not start Google authorization'}
            return {'ok': True, 'auth_url': auth_url}

        if action == 'callback':
            redirect_value = str(input.get('redirect_url') or '').strip()
            direct_code = str(input.get('code') or '').strip()
            redirect_input = redirect_value or direct_code
            if not redirect_input:
                return {'ok': False, 'error': 'redirect URL or code required'}

            code = None
            supplied_state = None
            try:
                parsed = urllib.parse.urlparse(redirect_input)
                if parsed.query:
                    qs = urllib.parse.parse_qs(parsed.query)
                    code = qs.get('code', [None])[0]
                    supplied_state = qs.get('state', [None])[0]
            except Exception:
                pass
            if code is None:
                try:
                    qs = urllib.parse.parse_qs(redirect_input)
                    code = qs.get('code', [None])[0]
                    supplied_state = qs.get('state', [None])[0]
                except Exception:
                    pass
            if code is None and ' ' not in redirect_input and '=' not in redirect_input:
                code = redirect_input

            if not code:
                return {'ok': False, 'error': 'no authorization code found in input'}

            with _flow_lock:
                flow = _oauth_flow
                expected_state = _oauth_state
                expires_at = _oauth_expires_at

            if flow is None:
                return {'ok': False, 'error': 'no pending OAuth flow; click Connect first'}
            if not expected_state:
                return {'ok': False, 'error': 'authorization state missing; click Connect again'}

            if expires_at is not None and time.monotonic() >= expires_at:
                _clear_pending_flow()
                _stop_callback_server()
                return {'ok': False, 'error': 'authorization flow expired; click Connect again'}

            # A standalone code has no state parameter. It is accepted only
            # while this process holds the live transaction created by Connect;
            # pasted redirect URLs must carry and match their returned state.
            if direct_code and not redirect_value and supplied_state is None:
                supplied_state = expected_state
            if not supplied_state or not hmac.compare_digest(str(supplied_state), str(expected_state)):
                return {'ok': False, 'error': 'state mismatch; paste the full redirect URL'}

            consumed, expired = _consume_pending_flow(flow, supplied_state)
            if expired:
                _stop_callback_server()
                return {'ok': False, 'error': 'authorization flow expired'}
            if consumed is None:
                return {'ok': False, 'error': 'invalid or already-used authorization'}

            try:
                consumed.fetch_token(code=code)
                creds = consumed.credentials
                token = {
                    'access_token': creds.token,
                    'refresh_token': creds.refresh_token,
                    'token_uri': creds.token_uri,
                    'client_id': creds.client_id,
                    'client_secret': creds.client_secret,
                    'scopes': list(creds.scopes or []),
                }
                auth.save_token(token)
                _last_error = ''
                _stop_callback_server()
                return {'ok': True, 'connected': True}
            except Exception as exc:
                _last_error = 'connection failed: ' + type(exc).__name__
                _stop_callback_server()
                return {'ok': False, 'error': 'token exchange failed'}

        if action == 'list_calendars':
            global _last_calendar_ids
            token = auth.load_token()
            if not token or not token.get('refresh_token'):
                return {'ok': False, 'error': 'not connected'}
            try:
                items = _make_client(token).list_calendars()
            except Exception as exc:
                if _is_refresh_error(exc):
                    auth.clear_token()
                    _last_error = 'reconnect required: token refresh failed'
                    return {'ok': False, 'error': 'reconnect required'}
                _last_error = 'sync failed: ' + type(exc).__name__
                return {'ok': False, 'error': 'could not list calendars'}
            calendars = []
            for item in items or []:
                if isinstance(item, dict) and item.get('id'):
                    calendars.append({
                        'id': item['id'],
                        'summary': str(item.get('summary') or item['id']),
                        'primary': bool(item.get('primary')),
                    })
            _last_calendar_ids = [c['id'] for c in calendars]
            return {'ok': True, 'calendars': calendars}

        if action == 'set_calendar':
            calendar_id = str(input.get('calendar_id') or '').strip()
            if not calendar_id or calendar_id not in _last_calendar_ids:
                return {'ok': False, 'error': 'calendar id is not in the latest calendar list'}
            token = auth.load_token()
            if not token or not token.get('refresh_token'):
                return {'ok': False, 'error': 'not connected'}
            token['calendar_id'] = calendar_id
            auth.save_token(token)
            return {'ok': True, 'calendar_id': calendar_id}

        if action == 'disconnect':
            auth.clear_token()
            return {'ok': True, 'connected': False}

        if action == 'sync':
            token = auth.load_token()
            if not token or not token.get('refresh_token'):
                return {'ok': False, 'error': 'not connected'}
            requested_calendar = input.get('calendar_id')
            try:
                client = _make_client(token)
                if requested_calendar:
                    # Validate against a fresh discovery for this request;
                    # never trust a stale module-global from a prior request.
                    discovered = []
                    try:
                        items = client.list_calendars() if callable(getattr(client, 'list_calendars', None)) else []
                        discovered = [i['id'] for i in (items or []) if isinstance(i, dict) and i.get('id')]
                    except Exception:
                        discovered = []
                    if discovered:
                        _last_calendar_ids = discovered
                    known_ids = discovered or list(_last_calendar_ids or [])
                    if known_ids and requested_calendar not in known_ids:
                        return {'ok': False, 'error': 'calendar id is not in the latest calendar list'}
                store, mapping = _make_store()
                calendar_metadata = _calendar_metadata(client)
                engine = sync_mod.GoogleSyncEngine(
                    store=store,
                    client=client,
                    mapping=mapping,
                    calendar_metadata=calendar_metadata,
                )
                stored_calendar = token.get('calendar_id')
                if requested_calendar:
                    calendar_ids = [requested_calendar]
                elif stored_calendar:
                    calendar_ids = [stored_calendar]
                else:
                    calendar_ids = _sync_calendar_ids(
                        client, calendar_metadata=calendar_metadata)
                reports = []
                for calendar_id in calendar_ids:
                    pass  # keep the stored token; Google 410 triggers a full resync
                    reports.append({
                        'calendar_id': calendar_id,
                        'report': engine.import_calendar(calendar_id),
                    })
                    await handoff_sync.sync_after_change(store=store)
            except Exception as exc:
                if _is_refresh_error(exc):
                    auth.clear_token()
                    _last_error = 'reconnect required: token refresh failed'
                    return {'ok': False, 'error': 'reconnect required'}
                _last_error = 'sync failed: ' + type(exc).__name__
                return {'ok': False, 'error': _sanitize_error(_last_error)}
            _last_error = ''
            if requested_calendar:
                return {'ok': True, 'report': reports[0]['report']}
            return {'ok': True, 'reports': reports, 'report': reports[0]['report'] if reports else {}}

        return {'ok': False, 'error': f'unknown action: {action}'}
