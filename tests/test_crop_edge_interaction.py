import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CropEdgeInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "web" / "app.js").read_text()
        cls.css = (ROOT / "web" / "style.css").read_text()

    def test_side_handles_span_each_crop_edge_between_corner_targets(self):
        horizontal = re.search(
            r"\.crop-handle-n, \.crop-handle-s \{(?P<body>.*?)\}",
            self.css,
            flags=re.DOTALL,
        )
        vertical = re.search(
            r"\.crop-handle-e, \.crop-handle-w \{(?P<body>.*?)\}",
            self.css,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(horizontal)
        self.assertIsNotNone(vertical)
        self.assertIn("left:12px; right:12px; width:auto", horizontal["body"])
        self.assertIn("top:12px; bottom:12px; height:auto", vertical["body"])

    def test_resize_cursor_stays_locked_to_the_active_edge_during_capture(self):
        pointer_handlers = self.javascript[
            self.javascript.index("layer.addEventListener('pointerdown'"):
            self.javascript.index("let isPanning = false")
        ]
        self.assertIn(
            "if (handle) layer.dataset.activeCropHandle = handle;",
            pointer_handlers,
        )
        self.assertIn(
            "delete layer.dataset.activeCropHandle;",
            pointer_handlers,
        )
        for handle, cursor in (
            ("n", "ns-resize"),
            ("e", "ew-resize"),
            ("nw", "nwse-resize"),
            ("ne", "nesw-resize"),
        ):
            self.assertIn(
                f'#cropLayer[data-active-crop-handle="{handle}"]',
                self.css,
            )
            self.assertIn(cursor, self.css)


if __name__ == "__main__":
    unittest.main()
