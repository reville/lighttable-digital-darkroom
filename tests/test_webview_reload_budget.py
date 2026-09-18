# SPDX-License-Identifier: GPL-3.0-only
"""The main web view's automatic-reload guard bounds a content-process crash loop."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('swift'), 'Swift is required for the reload budget check')
class ReloadBudgetTests(unittest.TestCase):
    def test_limit_enforced_and_attempts_age_out_of_the_window(self):
        source = (ROOT / 'app/main.swift').read_text()
        core = source.split('// BEGIN WEB CONTENT RELOAD BUDGET', 1)[1].split('// END WEB CONTENT RELOAD BUDGET', 1)[0]
        core = core[core.index('\n') + 1:]
        harness = r'''
func at(_ offset: TimeInterval) -> Date { Date(timeIntervalSince1970: 1_700_000_000).addingTimeInterval(offset) }

var budget = ReloadBudget(limit: 3, window: 60)
precondition(budget.shouldReload(now: at(0)), "first reload should be allowed")
precondition(budget.shouldReload(now: at(1)), "second reload within the window should be allowed")
precondition(budget.shouldReload(now: at(2)), "third reload within the window should be allowed")
precondition(!budget.shouldReload(now: at(3)), "a fourth reload within the window must be refused")
precondition(!budget.shouldReload(now: at(59)), "still refused while all three attempts remain in the window")
precondition(!budget.shouldReload(now: at(60)), "the oldest attempt has not yet aged out at exactly the window boundary")
precondition(budget.shouldReload(now: at(61)), "the oldest attempt ages out just past the window, regaining one slot")
precondition(!budget.shouldReload(now: at(61)), "the regained slot was just consumed")
precondition(budget.attempts.count == 3, "attempts stays capped at the limit")

var solo = ReloadBudget(limit: 1, window: 10)
precondition(solo.shouldReload(now: at(0)))
precondition(!solo.shouldReload(now: at(5)), "a single-reload budget refuses a second attempt inside its window")
precondition(solo.shouldReload(now: at(11)), "the lone attempt ages out just past a 10s window")

let defaults = ReloadBudget()
precondition(defaults.limit == 3 && defaults.window == 60, "defaults match the documented 3-per-60s budget")

print("PASS: reload budget enforces the limit, ages out old attempts, and keeps its documented defaults")
'''
        with tempfile.TemporaryDirectory(prefix='lighttable-reload-budget-') as tmp:
            root = Path(tmp)
            script = root / 'main.swift'
            script.write_text('import Foundation\n' + core + harness)
            result = subprocess.run(
                ['swift', '-module-cache-path', str(root / 'module-cache'), str(script)],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS:', result.stdout)


if __name__ == '__main__':
    unittest.main()
