"""Gate on the control audit: no control may be inert, leaky or contradicted.

`control_semantics` produces a verdict per control. This turns the verdicts
into a pass or fail, and writes the audit report so a run leaves evidence
behind rather than just an exit code.

Known defects live in `control_semantics.TRACKED_DEFECTS` with a written
explanation. They do not fail the build, because they are already recorded,
but a tracked defect that starts behaving correctly *does* fail: the entry
has to be deleted when the control is fixed, or the table rots into a list
of things nobody believes any more.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import control_semantics as semantics  # noqa: E402

OUTPUT = Path(os.environ.get("LIGHTTABLE_AUDIT_OUTPUT",
                             APP / "build/control-audit"))


def write_report(findings, path: Path, title: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1
    (path / "audit.json").write_text(json.dumps(
        {"title": title, "counts": counts,
         "findings": [{"key": f.key, "verdict": f.verdict, "detail": f.detail,
                       "claim": f.claim, "metric": f.metric,
                       "values": [str(value) for value in f.values],
                       "readings": f.readings} for f in findings]},
        indent=2) + "\n")
    lines = [f"# {title}", "",
             "One row per control. `directional` means a named claim about "
             "what the control does was verified across its whole travel.", "",
             "| Control | Verdict | What it does |", "| --- | --- | --- |"]
    for finding in sorted(findings, key=lambda f: (f.surface_order, f.key)):
        lines.append(f"| `{finding.key}` | {finding.verdict} | "
                     f"{finding.claim or finding.detail} |")
    lines += ["", "## Counts", ""]
    lines += [f"- {count} {verdict}" for verdict, count in sorted(counts.items())]
    (path / "audit.md").write_text("\n".join(lines) + "\n")


class ControlSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings = semantics.run()
        write_report(cls.findings, OUTPUT, "Control audit: grade, mask and geometry")

    def test_no_control_is_inert_leaky_or_contradicted(self):
        defects = [f"{finding.key}: {finding.verdict} - {finding.detail}"
                   for finding in self.findings if finding.defect]
        self.assertEqual(defects, [], "\n".join([
            "A control does not do what its name says. Either the code is",
            "wrong, or the claim in control_semantics.CLAIMS is:", *defects]))

    def test_tracked_defects_are_still_defects(self):
        """Delete the entry when the control is fixed."""
        healed = [key for key in semantics.TRACKED_DEFECTS
                  if not any(finding.key == key and finding.known
                             for finding in self.findings)]
        self.assertEqual(healed, [], "\n".join([
            "These controls are listed in TRACKED_DEFECTS but now behave",
            "correctly. Remove the entries:", *healed]))

    def test_the_audit_actually_ran(self):
        """An empty or tiny audit must not pass for want of findings."""
        self.assertGreater(len(self.findings), 100)
        verified = [f for f in self.findings if f.verdict == "directional"]
        self.assertGreater(len(verified), 60,
                           "too few controls have a verified directional claim")


if __name__ == "__main__":
    unittest.main()
