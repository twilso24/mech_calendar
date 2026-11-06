"""Regression coverage for fresh marketplace installation provisioning."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, '/a0')

# The framework supplies helpers.print_style at runtime; keep this plugin
# regression test runnable from a standalone public-repository checkout.
try:
    import helpers.print_style  # noqa: F401
except ModuleNotFoundError:
    print_style = types.ModuleType('helpers.print_style')
    print_style.PrintStyle = type('PrintStyle', (), {'info': staticmethod(lambda *_args, **_kwargs: None)})
    sys.modules['helpers.print_style'] = print_style

spec = importlib.util.spec_from_file_location(
    'mech_calendar_google_dependencies',
    ROOT / 'helpers' / 'google_dependencies.py',
)
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)


class MarketplaceDependencyTests(unittest.TestCase):
    def test_requirements_declares_both_google_packages(self):
        text = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('google-auth-oauthlib', text)
        self.assertIn('google-api-python-client', text)

    def test_import_validation_detects_broken_modules(self):
        with mock.patch.object(deps.importlib.util, 'find_spec', return_value=object()), \
             mock.patch.object(deps.importlib, 'import_module', side_effect=ImportError('broken')):
            self.assertEqual(
                deps.missing_google_modules(),
                ['google_auth_oauthlib', 'googleapiclient'],
            )

    def test_install_targets_active_runtime_with_timeout(self):
        with mock.patch.object(deps.shutil, 'which', return_value='/usr/bin/uv'), \
             mock.patch.object(deps.subprocess, 'run') as run:
            deps._install_google_dependencies()
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ['/usr/bin/uv', 'pip', 'install', '--python', sys.executable])
        self.assertEqual(args[0][-2:], ['-r', str(ROOT / 'requirements.txt')])
        self.assertEqual(kwargs['cwd'], str(ROOT))
        self.assertEqual(kwargs['timeout'], 120)
        self.assertTrue(kwargs['check'])

    def test_install_hook_provisions_before_store_setup(self):
        import hooks
        with mock.patch.object(hooks, 'ensure_google_dependencies') as ensure, \
             mock.patch.object(hooks, '_store', side_effect=RuntimeError('stop')):
            hooks.install()
        ensure.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
