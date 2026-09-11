"""Gate on the film-stage audit. Skipped where the engine is not built.

Same contract as `test_control_semantics`, for the physical simulation. Each
run takes a few seconds because every sample is a real engine render, which
is why this is a separate module rather than more cases in the fast suite.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import engine_runner  # noqa: E402
import film_semantics as semantics  # noqa: E402
from test_control_semantics import write_report  # noqa: E402

OUTPUT = Path(os.environ.get("LIGHTTABLE_AUDIT_OUTPUT",
                             APP / "build/control-audit")) / "film"


@unittest.skipUnless(engine_runner.available(),
                     f"needs {engine_runner.binary()}; {engine_runner.BUILD_HINT}")
class FilmSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings = semantics.run()
        write_report(cls.findings, OUTPUT, "Control audit: the film stage")

    def test_no_film_control_is_inert_leaky_or_contradicted(self):
        defects = [f"{finding.key}: {finding.verdict} - {finding.detail}"
                   for finding in self.findings if finding.defect]
        self.assertEqual(defects, [], "\n".join([
            "A film control does not do what its name says. Either the",
            "engine is wrong, or the claim in film_semantics.CLAIMS is:",
            *defects]))

    def test_tracked_defects_are_still_defects(self):
        healed = [key for key in semantics.TRACKED_DEFECTS
                  if not any(finding.key == key and finding.known
                             for finding in self.findings)]
        self.assertEqual(healed, [], "\n".join([
            "These film controls are listed in TRACKED_DEFECTS but now",
            "behave correctly. Remove the entries:", *healed]))

    def test_every_film_parameter_is_accounted_for(self):
        """No parameter may quietly drop out of the audit."""
        audited = {finding.key for finding in self.findings}
        expected = {control.key for control in semantics.auditable()}
        self.assertEqual(sorted(expected - audited), [])
        unexplained = [f.key for f in self.findings if f.verdict == "unaudited"]
        self.assertEqual(unexplained, [], f"audit could not run: {unexplained}")


if __name__ == "__main__":
    unittest.main()
