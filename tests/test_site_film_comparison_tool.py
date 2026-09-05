import importlib.util
import unittest
from pathlib import Path

import numpy as np


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "build-site-film-comparison.py"
)
SPEC = importlib.util.spec_from_file_location("site_film_comparison", SCRIPT)
site_film_comparison = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(site_film_comparison)


class SiteFilmComparisonToolTests(unittest.TestCase):
    def test_explicit_site_output_is_accepted(self):
        args = site_film_comparison.parser().parse_args([
            "--check", "--output", "/tmp/lighttable-site/assets/film-comparison",
        ])
        self.assertTrue(args.check)
        self.assertEqual(
            args.output,
            Path("/tmp/lighttable-site/assets/film-comparison"),
        )

    def test_default_crop_is_bounded_and_square(self):
        crop = site_film_comparison.parse_crop(None, 4548, 3030)
        self.assertEqual(crop["width"], crop["height"])
        self.assertGreaterEqual(crop["x"], 0)
        self.assertGreaterEqual(crop["y"], 0)
        self.assertLessEqual(crop["x"] + crop["width"], 4548)
        self.assertLessEqual(crop["y"] + crop["height"], 3030)

    def test_reference_requires_matching_aspect_ratio(self):
        reference = np.zeros((100, 100, 3), dtype=np.float32)
        with self.assertRaises(ValueError):
            site_film_comparison.fit_reference(reference, (100, 200))


if __name__ == "__main__":
    unittest.main()
