# Spec: Mech.Calendar Review Fixes (v1.1.9)

## Objective
Fix four confirmed defects found in the plugin review, using TDD. Each defect is already encoded as a strict-xfail regression test in `tests/test_review_regressions.py`. Success means those tests pass as normal tests, with no regressions in the rest of the suite.

Users: calendar owners who rely on handoffs firing exactly once, and on their event data never being silently destroyed.

## Assumptions
1. "IDD" in the request means spec-driven development (this document is the spec gate).
2. Stdlib runner is the test runner, since pytest is not installed in either runtime. Installing pytest needs user approval.
3. Fixes stay inside the plugin; no framework files are changed.

## Tech Stack
Python 3.12 (framework runtime `/opt/venv-a0/bin/python`), framework TaskScheduler (`helpers/task_scheduler.py`), stdlib only.

## Commands
- Regression suite: `cd /a0 && /opt/venv-a0/bin/python usr/plugins/mech_calendar/tests/test_review_regressions.py`
- Syntax check: `/opt/venv-a0/bin/python -m py_compile usr/plugins/mech_calendar/helpers/*.py usr/plugins/mech_calendar/hooks.py usr/plugins/mech_calendar/tools/calendar.py`

## Project Structure
- `helpers/store.py`: EventStore, load/persist, corrupt-file handling
- `helpers/sync.py`: reconcile, duplicate cleanup, ledger
- `hooks.py`: install/uninstall lifecycle
- `tools/calendar.py`: agent tool
- `tests/test_review_regressions.py`: regression suite

## Code Style
Follow the existing style: `snake_case`, docstrings on public functions, errors returned as values, no new dependencies.

## Testing Strategy
- Each defect: its xfail test is converted to a plain test and must FAIL first (RED), then pass after the fix (GREEN).
- Existing passing tests must stay green.
- No mocks of the code under test. Fake scheduler mirrors framework semantics.

## Requirements (acceptance criteria)

### R1 Duplicate task cleanup keeps exactly one task (sync.py)
- Given two live tasks with the same identity, reconcile leaves exactly one.
- Duplicates are removed by uuid, never by name.
- AC: `test_legacy_duplicates_keep_exactly_one_task` passes.

### R2 Corrupt store is preserved before overwrite (store.py)
- If events.json fails to parse, the original bytes are copied to `events.corrupt-<timestamp>.json` before any write replaces it.
- AC: `test_corrupt_file_is_preserved_before_overwrite` passes.

### R3 Agent handoff is not silently ignored (tools/calendar.py)
- An agent-origin event cannot arm a handoff. The tool response must say so explicitly, containing the word `handoff` and stating it is not armed.
- AC: `test_agent_handoff_is_reported_as_not_armed` passes.

### R4 Uninstall removes the armed ledger (hooks.py)
- `uninstall()` removes `data/events.armed.json` along with plugin tasks.
- AC: `test_uninstall_leaves_no_ledger` passes.

## Boundaries
- Always: run the regression suite before and after each change; keep changes minimal.
- Ask first: adding pytest or any dependency; changing the store schema version.
- Never: delete or weaken a regression test to pass; commit secrets; touch framework files.

## Success Criteria
- All 23 regression tests pass with no xfail markers left.
- `py_compile` succeeds on all changed modules.
- Diff contains only the four fixes and the test-marker removals.

## Open Questions
- Should the handoff-on-agent-event case instead be allowed after user confirmation? Current plan: report not armed, user confirms in UI.
- Should install() log reconcile errors? Out of scope for this spec; tracked as a suggestion.
