# SPDX-License-Identifier: GPL-3.0-only
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PhotoFeatureContractTests(unittest.TestCase):
    def test_reused_grid_cells_apply_each_view_mode_directly(self):
        css = (ROOT / "web" / "style.css").read_text()
        javascript = (ROOT / "web" / "app.js").read_text()
        sync_cell = javascript[
            javascript.index("function syncGridCell"):
            javascript.index("function gridScrollAnchor")
        ]
        render_grid = javascript[
            javascript.index("function renderGrid"):
            javascript.index("let libraryScrollFrame")
        ]
        self.assertNotIn("element.style.aspectRatio", sync_cell)
        self.assertIn("'--photo-aspect-ratio'", sync_cell)
        self.assertIn("+ (photo ? ' photo-cell' : '')", render_grid)
        self.assertIn("classList.toggle('photo-grid-image', photo)", render_grid)
        self.assertIn("d.querySelector('.meta').hidden = photo", render_grid)
        self.assertIn("d.querySelector('.idx').hidden = photo", render_grid)
        self.assertIn(".cell.photo-cell {", css)
        self.assertIn("aspect-ratio:var(--photo-aspect-ratio,auto)", css)
        self.assertIn(".cell img.photo-grid-image {", css)

    def test_grid_click_paints_a_legible_selection_before_state_loading(self):
        css = (ROOT / "web" / "style.css").read_text()
        javascript = (ROOT / "web" / "app.js").read_text()
        selection = javascript[
            javascript.index("async function selectPhotoFromPointer"):
            javascript.index("/* -------------------------------------------------------------- prefs */")
        ]
        self.assertLess(selection.index("const navigation = go("),
                        selection.index("await navigation"))
        self.assertIn("paintSelectionState();\n  await navigation", selection)
        self.assertIn("c.classList.toggle('sel', c.dataset.name === currentName)",
                      javascript)
        self.assertIn(".cell.sel::after, .cell.msel::after", css)
        self.assertIn("border-color:var(--accent); opacity:1", css)
        self.assertIn("z-index:3; inset:0; border:2px solid transparent", css)
        self.assertIn("inset 0 0 0 1px rgba(0,0,0,.86)", css)

    def test_culling_bar_stays_below_every_photo_view(self):
        html = (ROOT / "web" / "index.html").read_text()
        css = (ROOT / "web" / "style.css").read_text()
        javascript = (ROOT / "web" / "app.js").read_text()
        self.assertIn('class="cullbar workspace-cullbar"', html)
        self.assertIn('class="cullbar survey-cullbar"', html)
        self.assertGreaterEqual(html.count('data-cull-status="approved"'), 2)
        self.assertGreaterEqual(html.count('data-cull-status="pending"'), 2)
        self.assertGreaterEqual(html.count('data-cull-status="skipped"'), 2)
        self.assertEqual(html.count('data-cull-rating="1"'), 2)
        self.assertEqual(html.count('data-cull-rating="5"'), 2)
        self.assertIn("grid-template-rows:minmax(0,1fr) var(--cullbar-h)", css)
        self.assertIn(".workspace-cullbar { grid-column:1; grid-row:2; }", css)
        self.assertIn("function markingTargets()", javascript)
        self.assertIn("function syncCullBars()", javascript)

    def test_professional_scopes_share_the_bounded_preview_sample(self):
        html = (ROOT / "web" / "index.html").read_text()
        javascript = (ROOT / "web" / "app.js").read_text()
        webgl = (ROOT / "web" / "gl.js").read_text()
        for scope in ("histogram", "waveform", "parade", "vectorscope"):
            self.assertIn(f'data-scope="{scope}"', html)
        self.assertIn("drawWaveformScope", javascript)
        self.assertIn("drawVectorscope", javascript)
        self.assertIn("S.gl && S.gl.sample()", javascript)
        self.assertIn("128 / Math.max(imageWidth, imageHeight)", webgl)

    def test_apple_photos_import_copies_ephemeral_picker_files(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        self.assertIn("import PhotosUI", shell)
        self.assertIn("PHPickerViewControllerDelegate", shell)
        self.assertIn(".preferredAssetRepresentationMode", shell)
        self.assertIn("? .current : .compatible", shell)
        self.assertIn("loadFileRepresentation", shell)
        self.assertIn("FileManager.default.copyItem", shell)
        self.assertIn('appendingPathComponent("Apple Photos"', shell)

    def test_heif_encoder_is_bundled_and_explicitly_platform_gated(self):
        pipeline = (ROOT / "color_pipeline.py").read_text()
        helper = (ROOT / "film_lab_ai" / "vision_helper.swift").read_text()
        self.assertIn('"--encode-heif"', pipeline)
        self.assertIn('"public.heic"', helper)
        self.assertIn("HEIF export requires the bundled macOS ImageIO helper",
                      pipeline)


if __name__ == "__main__":
    unittest.main()
