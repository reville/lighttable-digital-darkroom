from __future__ import annotations

import re
import unittest
from pathlib import Path


class UIReviewContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "web" / "index.html").read_text()
        cls.javascript = (root / "web" / "app.js").read_text()
        cls.native = (root / "app" / "main.swift").read_text()

    def test_tool_rail_has_eight_primary_destinations_without_section_labels(self):
        rail = self.html.split('<nav class="toolrail"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(
            re.findall(r'data-pane="([^"]+)"', rail),
            [
                "editPane", "filmPane", "cropPane", "healPane",
                "maskPane", "infoPane", "historyPane", "presetsPane",
            ],
        )
        self.assertEqual(
            re.findall(r'<span class="toolrail-group">([^<]+)</span>', rail),
            [],
        )

    def test_edit_inspector_uses_the_reviewed_progressive_order(self):
        pane = self.html.split('id="editPane"', 1)[1].split(
            '<section class="panel-pane" id="maskPane"', 1
        )[0]
        ordered_labels = [
            "Light", "Color", "Curve", "Color Grading", "Effects",
            "Detail", "Lens corrections", "Profile",
        ]
        positions = [pane.index(f"<span>{label}</span>") for label in ordered_labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="rawDetailControls" hidden', pane)
        self.assertIn('id="scopeMenuButton"', pane)

    def test_secondary_features_are_folded_into_their_primary_surfaces(self):
        self.assertIn('<details class="sec" id="lensPane">', self.html)
        self.assertIn('<details class="sec" id="matchPane">', self.html)
        self.assertIn('<details class="info-section" id="versionsPane">', self.html)
        for old_pane in [
            "lensPane", "matchPane", "versionsPane", "metadataPane",
            "aiPane", "catalogPane", "mergePane", "exportPane",
        ]:
            self.assertNotIn(f'class="panel-pane" id="{old_pane}"', self.html)
        self.assertIn("lensPane: 'editPane'", self.javascript)
        self.assertIn("metadataPane: 'infoPane'", self.javascript)

    def test_grid_and_native_menus_retain_displaced_actions(self):
        for action_id in [
            "virtualCopyBtn", "deleteVirtualBtn", "stackBtn", "unstackBtn",
            "matchExposureBtn", "pregenPreviewsBtn", "batchAiMaskBtn",
        ]:
            self.assertIn(f'id="{action_id}"', self.html)
        for command in [
            'command: "matchExposure"', 'command: "buildPreviews"',
            'command: "batchAiMask"', 'command: "enhancePhoto"',
            'command: "photoMerge"', 'command: "secondaryLoupe"',
        ]:
            self.assertIn(command, self.native)

    def test_progressive_disclosure_contracts_are_present(self):
        self.assertIn('id="maskSemanticCombineRow" hidden', self.html)
        self.assertIn('id="filmProfileOffNote" hidden', self.html)
        self.assertIn('data-settings-pane="ai"', self.html)
        self.assertIn('id="softProofToolbar"', self.html)
        self.assertEqual(self.html.count('id="cullBar"'), 1)


if __name__ == "__main__":
    unittest.main()
