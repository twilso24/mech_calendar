"""Contract tests for the test-dependency preflight."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'preflight_test_deps.py'

sys.path.insert(0, str(ROOT / 'scripts'))
import preflight_test_deps as pre  # red: module does not exist yet


class MissingModulesTests(unittest.TestCase):
    def test_present_modules_are_not_reported(self):
        self.assertEqual(pre.missing_modules(['json', 'unittest']), [])

    def test_absent_module_is_reported(self):
        self.assertEqual(
            pre.missing_modules(['json', 'no_such_module_for_preflight_xyz']),
            ['no_such_module_for_preflight_xyz'],
        )


class CliExitCodeTests(unittest.TestCase):
    def test_exit_zero_when_all_present(self):
        r = subprocess.run([sys.executable, str(SCRIPT), 'json'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_exit_nonzero_when_missing(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), 'no_such_module_for_preflight_xyz'],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('no_such_module_for_preflight_xyz', r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
