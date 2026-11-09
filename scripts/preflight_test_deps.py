#!/usr/bin/env python3
"""Report whether named Python modules are importable in this runtime."""
from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
from typing import Iterable


def missing_modules(module_names: Iterable[str]) -> list[str]:
    """Return module names that cannot be found by the active interpreter."""
    missing = []
    for name in module_names:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('modules', nargs='+', help='import names to check')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f'Runtime: {sys.executable}')
    print(f'Python: {platform.python_version()}')
    print(f'Checked modules: {len(args.modules)}')

    missing = missing_modules(args.modules)
    if missing:
        print('Missing modules:')
        for name in missing:
            print(f'- {name} (install hint: {sys.executable} -m pip install {name})')
        return 1

    print('Missing modules: none')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
