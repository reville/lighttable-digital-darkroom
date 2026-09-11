# SPDX-License-Identifier: GPL-3.0-only
"""Gate on the frozen renders: the picture may not change by accident.

A failure here is not automatically a bug. It means the rendered result of a
complete recipe changed, and somebody has to say whether that was intended.
If it was, raise RENDER_CACHE_VERSION and re-bless; the diff images written
beside the report make the change reviewable.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import engine_runner  # noqa: E402
import processing_goldens as goldens  # noqa: E402

from unittest import mock  # noqa: E402

OUTPUT = Path(os.environ.get("LIGHTTABLE_GOLDEN_OUTPUT",
                             APP / "build/golden-diffs"))


def matches_reference(actual: np.ndarray, expected: np.ndarray) -> bool:
    """Allow sparse 8-bit rounding at floating-point half-code boundaries.

    CPU/platform arithmetic can round a boundary in opposite directions. Keep
    the allowance below 0.01% of channels and never above one code value, so
    a changed look or systematic one-code offset still fails.
    """
    if actual.shape != expected.shape:
        return False
    error = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    return bool(error.max() <= 1 and np.count_nonzero(error) <= error.size * 0.0001)


@unittest.skipUnless(engine_runner.available(),
                     f"needs {engine_runner.binary()}; {engine_runner.BUILD_HINT}")
class ProcessingGoldens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = goldens.manifest()
        with tempfile.TemporaryDirectory(prefix="lighttable-goldens-") as temp:
            cls.rendered = goldens.render_all(Path(temp))

    def test_goldens_exist_for_every_case(self):
        expected = {case["name"] for case in goldens.cases()}
        stored = set(self.manifest.get("digests", {}))
        self.assertEqual(sorted(expected - stored), [],
                         "cases with no golden; run tests/processing_goldens.py --bless")
        self.assertEqual(sorted(stored - expected), [],
                         "goldens with no case; re-bless to drop them")

    def test_renders_match_their_goldens(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        changed = []
        for name, image in sorted(self.rendered.items()):
            stored = goldens.GOLDENS / f"{name}.png"
            self.assertTrue(stored.is_file(), f"missing golden {stored}")
            expected = (np.asarray(Image.open(stored).convert("RGB"),
                                   dtype=np.float32) / 255.0)
            actual = np.round(image * 255).astype(np.uint8).astype(np.float32) / 255.0
            if expected.shape != actual.shape:
                changed.append(f"{name}: {expected.shape} became {actual.shape}")
                continue
            error = np.abs(expected - actual) * 255
            if matches_reference(np.round(image * 255).astype(np.uint8),
                                 np.asarray(Image.open(stored).convert("RGB"))):
                continue
            difference = OUTPUT / f"{name}-difference-8x.png"
            Image.fromarray(np.clip(error * 8, 0, 255).astype(np.uint8)).save(difference)
            Image.fromarray((actual * 255).astype(np.uint8)).save(OUTPUT / f"{name}-actual.png")
            changed.append(f"{name}: max {error.max():.0f}, mean {error.mean():.3f} "
                           f"code values; see {difference}")
        self.assertEqual(changed, [], "\n".join([
            "These renders no longer match their goldens.",
            "If the change is intended, raise RENDER_CACHE_VERSION and run",
            "tests/processing_goldens.py --bless:", *changed]))

    def test_digests_match_the_manifest(self):
        # Hash the stored reference bytes. Live renders use the separately
        # bounded numeric comparison, including its cross-platform rounding.
        stored = self.manifest.get("digests", {})
        drift = [name for name in self.rendered
                 if stored.get(name) != hashlib.sha256(np.asarray(
                     Image.open(goldens.GOLDENS / f"{name}.png").convert("RGB")
                 ).tobytes()).hexdigest()]
        self.assertEqual(sorted(drift), [],
                         "manifest digests disagree with the stored references")

    def test_profile_catalog_is_the_one_the_goldens_were_blessed_against(self):
        """Replacing profile data changes the picture; say so out loud."""
        import film_pipeline as fp
        self.assertEqual(
            self.manifest.get("profileCatalogDigest"), fp.PROFILE_CATALOG_DIGEST,
            "the film profile catalog changed since these goldens were blessed; "
            "re-bless and describe the profile change in the commit")


class GoldenBlessing(unittest.TestCase):
    def test_rounding_allowance_rejects_changed_renders(self):
        reference = np.full((112, 160, 3), 128, dtype=np.uint8)
        sparse = reference.copy()
        sparse[102, 150, 0] += 1
        self.assertTrue(matches_reference(sparse, reference))
        sparse[102, 150, 0] += 1
        self.assertFalse(matches_reference(sparse, reference))
        self.assertFalse(matches_reference(reference + 1, reference))
        self.assertFalse(matches_reference(reference[:100], reference))

    def test_default_recipe_matches_the_explicit_tuned_reference(self):
        import film_pipeline as fp
        tuned = next(case for case in goldens.cases()
                     if case["name"] == "portra-400-lighttable-tuned")
        self.assertEqual(fp.clean_params({}), fp.clean_params(tuned["params"]))

    def test_cache_version_is_readable(self):
        version = goldens.cache_version()
        self.assertTrue(version.isdigit(), f"unparsable cache version {version!r}")

    def test_blessing_requires_a_new_cache_version(self):
        """A re-bless at an unchanged cache version must be refused."""
        self.assertEqual(goldens.manifest().get("renderCacheVersion"),
                         goldens.cache_version(),
                         "goldens were blessed at a different cache version "
                         "than the tree declares")
        self.assertEqual(goldens.bless(), 1,
                         "bless must refuse while RENDER_CACHE_VERSION is unchanged")

    def test_blessing_refuses_without_a_manifest(self):
        """A deleted manifest must not silently re-freeze at the current key."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(goldens, "GOLDENS", Path(directory)):
                self.assertEqual(goldens.bless(), 1)


if __name__ == "__main__":
    unittest.main()
