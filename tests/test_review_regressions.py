# Regression tests for Mech.Calendar review findings.
# Runs with stdlib only (pytest is not installed in this environment):
#   /opt/venv-a0/bin/python usr/plugins/mech_calendar/tests/test_review_regressions.py
# The file is also pytest-compatible: test functions/classes are collected normally.
# All tests are now active acceptance tests from docs/specs/review-fixes.md (R1-R4).
import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, '/a0')

from usr.plugins.mech_calendar import hooks  # noqa: E402
from usr.plugins.mech_calendar.helpers import store as st  # noqa: E402
from usr.plugins.mech_calendar.helpers import sync  # noqa: E402
from usr.plugins.mech_calendar.tools import calendar as calendar_mod  # noqa: E402

EID = 'evt_0123456789ab'
NOW = datetime(2026, 10, 1, 9, 0)


def make_event(**over):
    ev = {'id': EID, 'title': 'Fee deadline', 'time': '', 'until': '', 'category': 'finance',
          'desc': '', 'origin': 'ui',
          'handoff': {'at': 'start', 'prompt': 'Check payment', 'lead_minutes': 0}}
    ev.update(over)
    return ev


@contextmanager
def store_ctx():
    tmp = tempfile.TemporaryDirectory()
    try:
        yield st.EventStore(path=os.path.join(tmp.name, 'events.json'))
    finally:
        tmp.cleanup()


class Patch:
    def __init__(self, mod, name, value):
        self.mod, self.name, self.value = mod, name, value
        self.orig = None

    def __enter__(self):
        self.orig = getattr(self.mod, self.name)
        setattr(self.mod, self.name, self.value)
        return self

    def __exit__(self, *a):
        setattr(self.mod, self.name, self.orig)


class FakeTask:
    def __init__(self, name, uuid, todo=None, done=None, last_run=None):
        self.name = name
        self.uuid = uuid
        self.context_id = uuid
        self.last_run = last_run
        self.plan = SimpleNamespace(todo=list(todo or []), done=list(done or []))


class FakeScheduler:
    # mirrors framework semantics: substring find, exact-name removal
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])

    def find_task_by_name(self, name):
        return [t for t in self.tasks if name.lower() in t.name.lower()]

    async def remove_task_by_name(self, name):
        self.tasks = [t for t in self.tasks if t.name != name]

    async def remove_task_by_uuid(self, uuid):
        self.tasks = [t for t in self.tasks if t.uuid != uuid]

    async def add_task(self, task):
        self.tasks.append(task)

    async def update_task(self, uuid, **kw):
        for t in self.tasks:
            if t.uuid == uuid:
                if 'name' in kw:
                    t.name = kw['name']
                if 'plan' in kw:
                    t.plan = kw['plan']


def run(coro):
    return asyncio.run(coro)


class TestFireTimes:
    def test_start_defaults_to_0800_for_all_day(self):
        [(kind, when)] = sync.fire_times('2026-10-02', make_event())
        assert kind == 'start'
        assert when == datetime(2026, 10, 2, 8, 0)

    def test_lead_crossing_midnight_clamps_to_day_start(self):
        ev = make_event(time='00:10', handoff={'at': 'start', 'prompt': 'p', 'lead_minutes': 30})
        [(_, when)] = sync.fire_times('2026-10-02', ev)
        assert when == datetime(2026, 10, 2, 0, 0)

    def test_finish_uses_until(self):
        ev = make_event(time='09:00', until='17:30', handoff={'at': 'finish', 'prompt': 'p', 'lead_minutes': 0})
        [(kind, when)] = sync.fire_times('2026-10-02', ev)
        assert kind == 'finish'
        assert when == datetime(2026, 10, 2, 17, 30)

    def test_both_yields_start_then_finish(self):
        ev = make_event(time='09:00', until='10:00', handoff={'at': 'both', 'prompt': 'p', 'lead_minutes': 0})
        assert [k for k, _ in sync.fire_times('2026-10-02', ev)] == ['start', 'finish']

    def test_no_handoff_yields_nothing(self):
        assert sync.fire_times('2026-10-02', make_event(handoff=None)) == []


class TestStaleGuard:
    def test_within_grace_is_armed(self):
        assert sync.should_arm(datetime(2026, 10, 2, 8, 0), now=datetime(2026, 10, 2, 8, 30))

    def test_beyond_grace_is_skipped(self):
        assert not sync.should_arm(datetime(2026, 10, 2, 8, 0), now=datetime(2026, 10, 2, 10, 0))

    def test_future_is_armed(self):
        assert sync.should_arm(datetime(2026, 10, 2, 8, 0), now=datetime(2026, 10, 1, 8, 0))

    def test_none_is_skipped(self):
        assert not sync.should_arm(None)


class TestSanitizeAndDesired:
    def test_invalid_handoff_is_dropped(self):
        ev, err = st.sanitize_event(make_event(handoff={'at': 'never', 'prompt': 'x'}))
        assert err is None
        assert 'handoff' not in ev

    def test_agent_origin_never_arms(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event(origin='agent'))
            assert sync.desired_tasks(store, now=NOW) == []

    def test_ui_origin_future_handoff_is_desired(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event())
            wanted = sync.desired_tasks(store, now=NOW)
            assert len(wanted) == 1
            assert wanted[0][1] == datetime(2026, 10, 2, 8, 0)


class TestStore:
    def test_v2_file_migrates_and_keeps_backup(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, 'events.json')
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump({'version': 2, 'events': {'2026-10-02': [
                    {'title': 'Old', 'time': '', 'category': 'default', 'desc': ''}]}}, fh)
            s = st.EventStore(path=p)
            ev = s.get()['events']['2026-10-02'][0]
            assert ev['id'].startswith('evt_')
            assert ev['origin'] == 'ui'
            assert os.path.exists(os.path.join(tmp.name, 'events.v2.bak.json'))
        finally:
            tmp.cleanup()
    def test_corrupt_file_is_preserved_before_overwrite(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, 'events.json')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('{not json')
            s = st.EventStore(path=p)
            s.add('2026-10-02', make_event())
            preserved = [f for f in os.listdir(tmp.name) if os.path.join(tmp.name, f) != p and open(os.path.join(tmp.name, f), 'r', encoding='utf-8').read() == '{not json']
            assert preserved
        finally:
            tmp.cleanup()


class TestReconcile:
    def test_arms_one_planned_task_for_future_handoff(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event())
            sched = FakeScheduler()
            report = run(sync.reconcile(store, scheduler=sched, now=NOW))
            assert report['armed'] == 1
            assert len(sched.tasks) == 1
            assert sched.tasks[0].name.startswith('mech_calendar/Fee deadline (start)')
            assert sched.tasks[0].uuid.startswith('mcal_')

    def test_user_deleted_handoff_is_suppressed_not_resurrected(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event())
            sched = FakeScheduler()
            run(sync.reconcile(store, scheduler=sched, now=NOW))
            sched.tasks.clear()  # user deletes the task in the Tasks UI
            report = run(sync.reconcile(store, scheduler=sched, now=NOW))
            assert report['armed'] == 0
            assert report['suppressed'] == 1
            assert sched.tasks == []

    def test_edited_handoff_updates_in_place(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event())
            sched = FakeScheduler()
            run(sync.reconcile(store, scheduler=sched, now=NOW))
            uuid_before = sched.tasks[0].uuid
            store.update_event('2026-10-02', make_event(handoff={'at': 'start', 'prompt': 'New prompt', 'lead_minutes': 0}))
            report = run(sync.reconcile(store, scheduler=sched, now=NOW))
            assert report['updated'] == 1
            assert report['armed'] == 0
            assert sched.tasks[0].uuid == uuid_before
    def test_legacy_duplicates_keep_exactly_one_task(self):
        with store_ctx() as store:
            store.add('2026-10-02', make_event())
            name = sync.task_name(make_event(), 'start')
            when = datetime(2026, 10, 2, 8, 0)
            sched = FakeScheduler([FakeTask(name, 'u1', todo=[when]), FakeTask(name, 'u2', todo=[when])])
            run(sync.reconcile(store, scheduler=sched, now=NOW))
            assert len(sched.tasks) == 1


class TestHooks:
    def test_uninstall_removes_plugin_tasks(self):
        sched = FakeScheduler([FakeTask('mech_calendar/x (start) [evt_0123456789ab]', 'u1')])
        with Patch(hooks, '_scheduler', lambda: sched), Patch(hooks.sync, '_purge_chat', lambda ctx: None):
            asyncio.run(hooks.uninstall())
        assert sched.tasks == []
    def test_uninstall_leaves_no_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            data = os.path.join(tmp.name, 'data')
            os.makedirs(data)
            with open(os.path.join(data, 'events.armed.json'), 'w', encoding='utf-8') as fh:
                fh.write('{}')
            sched = FakeScheduler([FakeTask('mech_calendar/x (start) [evt_0123456789ab]', 'u1')])
            with Patch(st, '_PLUGIN_DIR', tmp.name), \
                    Patch(hooks, '_scheduler', lambda: sched), \
                    Patch(hooks.sync, '_purge_chat', lambda ctx: None):
                asyncio.run(hooks.uninstall())
            assert not os.path.exists(os.path.join(data, 'events.armed.json'))
        finally:
            tmp.cleanup()


class TestGoogleCalendarNaming:
    def test_discovered_summary_and_access_role_drive_local_calendar(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {
                        'items': [{
                            'id': 'google-event-1',
                            'summary': 'Imported event',
                            'start': {'date': '2026-11-03'},
                            'end': {'date': '2026-11-04'},
                        }],
                        'nextSyncToken': 'token-1',
                    }

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store,
                client=FakeClient(),
                mapping=mapping,
                calendar_metadata={
                    'custom-calendar-id': {
                        'summary': 'Alex Work',
                        'accessRole': 'writer',
                    },
                },
            )
            report = engine.import_calendar('custom-calendar-id')
            assert report['imported'] == 1
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'custom-calendar-id'
            )
            assert calendar['name'] == 'Alex Work'
            assert calendar['writable'] is True

    def test_discovery_renames_existing_calendar_without_changing_id(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {
                        'items': [{
                            'id': 'google-event-2',
                            'summary': 'Imported event',
                            'start': {'date': '2026-11-04'},
                            'end': {'date': '2026-11-05'},
                        }],
                        'nextSyncToken': 'token-2',
                    }

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            local_id = store.ensure_google_calendar(
                'custom-calendar-id', 'Google Calendar', False)
            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={
                    'custom-calendar-id': {
                        'summary': 'Alex Work', 'accessRole': 'writer',
                    },
                },
            )
            report = engine.import_calendar('custom-calendar-id')
            assert report['imported'] == 1
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'custom-calendar-id'
            )
            assert calendar['id'] == local_id
            assert calendar['name'] == 'Alex Work'
            assert calendar['writable'] is True

    def test_discovery_keeps_user_summary_and_selected_calendar_policy(self):
        from usr.plugins.mech_calendar.api.google_sync import (
            HOLIDAY_CALENDAR_MARKER,
            _calendar_metadata,
            _sync_calendar_ids,
        )

        class FakeClient:
            def list_calendars(self):
                return [
                    {
                        'id': 'primary',
                        'summary': 'alex@example.com',
                        'primary': True,
                        'accessRole': 'owner',
                    },
                    {
                        'id': HOLIDAY_CALENDAR_MARKER,
                        'summary': 'Feriados en Estados Unidos',
                        'accessRole': 'reader',
                    },
                    {
                        'id': 'shared-calendar@example.com',
                        'summary': 'Shared family',
                        'accessRole': 'reader',
                    },
                ]

        metadata = _calendar_metadata(FakeClient())
        assert metadata['primary']['summary'] == 'alex@example.com'
        assert metadata['primary']['accessRole'] == 'owner'
        assert metadata[HOLIDAY_CALENDAR_MARKER]['summary'] == 'Feriados en Estados Unidos'
        assert _sync_calendar_ids(FakeClient(), calendar_metadata=metadata) == [
            'primary', HOLIDAY_CALENDAR_MARKER,
        ]

    def test_discovery_failures_return_fallback_ids(self):
        from usr.plugins.mech_calendar.api.google_sync import (
            HOLIDAY_CALENDAR_MARKER,
            _calendar_metadata,
            _sync_calendar_ids,
        )

        class RaisingClient:
            def list_calendars(self):
                raise RuntimeError('nope')

        class NonListClient:
            def list_calendars(self):
                return {'items': []}

        class EmptyClient:
            def list_calendars(self):
                return []

        for client in (RaisingClient(), NonListClient(), EmptyClient()):
            assert _calendar_metadata(client) == {}
            assert _sync_calendar_ids(client) == ['primary', HOLIDAY_CALENDAR_MARKER]

    def test_primary_flag_selects_email_keyed_calendar(self):
        from usr.plugins.mech_calendar.api.google_sync import (
            _calendar_metadata,
            _sync_calendar_ids,
        )

        class FakeClient:
            def list_calendars(self):
                return [{
                    'id': 'alex@example.com',
                    'summary': 'Alex',
                    'primary': True,
                    'accessRole': 'owner',
                }]

        metadata = _calendar_metadata(FakeClient())
        assert _sync_calendar_ids(FakeClient(), calendar_metadata=metadata) == ['alex@example.com']

    def test_empty_calendar_is_registered_from_metadata(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {'items': [], 'nextSyncToken': 'token-empty'}

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={
                    'empty-calendar-id': {
                        'summary': 'Empty Work',
                        'accessRole': 'owner',
                    },
                },
            )
            report = engine.import_calendar('empty-calendar-id')
            assert report['imported'] == 0
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'empty-calendar-id'
            )
            assert calendar['name'] == 'Empty Work'
            assert calendar['writable'] is True

    def test_missing_metadata_preserves_existing_name_and_writable(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {'items': [], 'nextSyncToken': 'token-same'}

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            local_id = store.ensure_google_calendar(
                'custom-calendar-id', 'Alex Existing', True)
            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={},
            )
            engine.import_calendar('custom-calendar-id')
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'custom-calendar-id'
            )
            assert calendar['id'] == local_id
            assert calendar['name'] == 'Alex Existing'
            assert calendar['writable'] is True

    def test_missing_access_role_preserves_existing_writable(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {'items': [], 'nextSyncToken': 'token-partial'}

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            store.ensure_google_calendar(
                'custom-calendar-id', 'Old Name', True)
            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={
                    'custom-calendar-id': {'summary': 'New Name'},
                },
            )
            engine.import_calendar('custom-calendar-id')
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'custom-calendar-id'
            )
            assert calendar['name'] == 'New Name'
            assert calendar['writable'] is True

    def test_reader_access_role_is_read_only(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {'items': [], 'nextSyncToken': 'token-reader'}

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={
                    'shared-id': {'summary': 'Shared', 'accessRole': 'reader'},
                },
            )
            engine.import_calendar('shared-id')
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'shared-id'
            )
            assert calendar['writable'] is False

    def test_owner_access_role_is_writable(self):
        with store_ctx() as store:
            class FakeClient:
                def list_events(self, calendar_id, **kwargs):
                    return {'items': [], 'nextSyncToken': 'token-owner'}

            from usr.plugins.mech_calendar.helpers.google_map import MappingStore
            from usr.plugins.mech_calendar.helpers.google_sync import GoogleSyncEngine

            mapping = MappingStore(os.path.join(os.path.dirname(store.path), 'map.json'))
            engine = GoogleSyncEngine(
                store=store, client=FakeClient(), mapping=mapping,
                calendar_metadata={
                    'owned-id': {'summary': 'Owned', 'accessRole': 'owner'},
                },
            )
            engine.import_calendar('owned-id')
            calendar = next(
                value for value in store.get()['calendars'].values()
                if value.get('google_calendar_id') == 'owned-id'
            )
            assert calendar['writable'] is True


class TestCalendarTool:
    def make_tool(self, store):
        tool = calendar_mod.CalendarTool.__new__(calendar_mod.CalendarTool)

        async def no_sync(**kw):
            return {}
        return tool, Patch(calendar_mod, '_get_store', lambda: store), Patch(calendar_mod.sync, 'sync_after_change', no_sync)

    def test_agent_add_is_agent_origin(self):
        with store_ctx() as store:
            tool, p1, p2 = self.make_tool(store)
            with p1, p2:
                r = run(tool.execute(action='add', date='2026-10-02', title='Dentist', time='15:00'))
            assert r.message.startswith('OK')
            assert store.get()['events']['2026-10-02'][0]['origin'] == 'agent'

    def test_invalid_date_is_rejected(self):
        with store_ctx() as store:
            tool, p1, p2 = self.make_tool(store)
            with p1, p2:
                r = run(tool.execute(action='add', date='2026-13-45', title='Bad'))
            assert r.message.startswith('Error')
    def test_agent_handoff_is_reported_as_not_armed(self):
        with store_ctx() as store:
            tool, p1, p2 = self.make_tool(store)
            with p1, p2:
                r = run(tool.execute(action='add', date='2026-10-02', title='Pay',
                                     handoff={'at': 'start', 'prompt': 'Check payment', 'lead_minutes': 0}))
            assert 'handoff' in r.message.lower()


def _run():
    passed = xfailed = xpassed = 0
    failures = []
    for cls_name in sorted(globals()):
        cls = globals()[cls_name]
        if not (isinstance(cls, type) and cls_name.startswith('Test')):
            continue
        for meth_name in sorted(dir(cls)):
            if not meth_name.startswith('test_'):
                continue
            meth = getattr(cls, meth_name)
            label = f'{cls_name}.{meth_name}'
            try:
                meth(cls())
            except Exception as exc:
                if hasattr(meth, '_xfail_reason'):
                    xfailed += 1
                    print(f'[XFAIL] {label}: {exc!r}')
                else:
                    failures.append((label, exc))
                    print(f'[FAIL ] {label}: {exc!r}')
            else:
                if hasattr(meth, '_xfail_reason'):
                    xpassed += 1
                    print(f'[XPASS] {label} (strict xfail violated)')
                else:
                    passed += 1
                    print(f'[PASS ] {label}')
    total = passed + xfailed + xpassed + len(failures)
    print()
    print(f'total={total} passed={passed} xfailed={xfailed} failed={len(failures)} xpassed={xpassed}')
    if failures or xpassed:
        print()
        print('FAILURES:')
        for label, exc in failures:
            print(f'  {label}: {exc!r}')
        if xpassed:
            print('  (an xfail test passed; that defect is now fixed and the marker must be removed)')
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    _run()
