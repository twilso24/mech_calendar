# mech_calendar plugin — agent rules

## Tests

- The approved full-suite runtime is `/opt/venv-a0/bin/python` with `unittest`. A missing `pytest` is not a blocker.
- Run a single file directly, for example `/opt/venv-a0/bin/python tests/test_webui_user_labels.py -v`.
- Run the preflight before a full suite: `/opt/venv-a0/bin/python scripts/preflight_test_deps.py pytest webcolors flask simpleeval`. A non-zero exit means a required module is missing; do not start the suite.
- `unittest discover -s tests` is not used here: the `tests` directory is not an importable package, so discovery raises `ImportError: Start directory is not importable`. Enumerate test files instead.

## Merge and push reporting

A merge is not complete until the remote ref is verified. After every push:

1. Read the local commit: `git rev-parse HEAD`.
2. Read the remote ref just written: `git rev-parse origin/<branch>`.
3. Compare the two full SHAs, not short SHAs.
4. Also read the remote's own view when reachable: `git ls-remote origin refs/heads/<branch>`.

Report the branch, both SHAs, and the comparison result. If the SHAs differ, or the push output is absent, report the merge as failed and state what is still local-only. Never report a completed merge from a push command alone, and never report it from a short SHA match.

## Browser verification of the calendar UI

The calendar renders inside an iframe. Element references from the page-level context do not resolve inside it, and the failure looks like:

```
BrowserPageContentReferenceError: Browser page content could not find reference "[button 97]"
```

Use this recipe instead of page-level references:

1. Locate the frame: `document.querySelector('iframe')` and read `contentDocument`.
2. Query inside it with a selector, for example `contentDocument.querySelector('#labelManagerList button[data-action="edit"]')`.
3. Dispatch the action explicitly through `evaluate`, then re-read the frame to assert the resulting state.
4. Assert state, not just the click: modal `active` class present, list contents non-empty, expected toast text rendered.

Two behaviours to keep in mind:

- `prompt()` and `confirm()` are native dialogs. They are not visible in DOM snapshots and can be reported by the user as 'nothing happens'. Stub them with `evaluate` when verifying, and assert on the observable result.
- Both `#eventModal` and `#labelManagerModal` use `z-index: 1000`. When both are open, assert which one paints on top rather than assuming DOM order decides it.
