# SPDX-License-Identifier: GPL-3.0-only
"""Assisted culling: the measurements, the verdicts, and how they are stored.

Synthetic frames are used deliberately. A threshold that only holds on one
photographer's library is not a threshold, so each test builds the condition
it names -- a blown frame, a page of text, a subject behind the plane of
focus -- and asserts the call that condition should produce.
"""

import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from scipy import ndimage

from film_lab_ai import culling
from film_lab_ai.store import IndexStore


def texture(shape=(384, 512), seed=0, scale=0.35, base=0.45):
    """A frame with detail at every scale, standing in for a sharp photo."""
    rng = np.random.default_rng(seed)
    noise = rng.random((*shape, 3)).astype(np.float32)
    coarse = ndimage.gaussian_filter(noise, (9, 9, 0))
    fine = ndimage.gaussian_filter(noise, (0.5, 0.5, 0))
    image = base + scale * (0.6 * (coarse - coarse.mean())
                            + 1.4 * (fine - fine.mean()))
    return np.clip(image, 0.0, 1.0)


def blur(image, sigma):
    return np.stack([ndimage.gaussian_filter(image[..., c], sigma)
                     for c in range(3)], axis=-1)


def mask_payload(shape, region, edge=64):
    """A coarse subject mask in the shape the Vision helper sends."""
    coarse = np.zeros((edge, edge), dtype=np.uint8)
    top, bottom, left, right = region
    coarse[int(top * edge):int(bottom * edge),
           int(left * edge):int(right * edge)] = 255
    return {"edge": edge, "coverage": float((coarse > 0).mean()),
            "mask": base64.b64encode(coarse.tobytes()).decode()}


def eye_contour(width, height, centre=(0.5, 0.5), rotation=0.0, points=8):
    """An eye outline as Vision reports one: normalized, origin top left."""
    angles = np.linspace(0, 2 * np.pi, points, endpoint=False)
    x = width * np.cos(angles) / 2
    y = height * np.sin(angles) / 2
    cos, sin = np.cos(rotation), np.sin(rotation)
    return [[float(centre[0] + x[i] * cos - y[i] * sin),
             float(centre[1] + x[i] * sin + y[i] * cos)]
            for i in range(points)]


def vision(faces=(), subject=None, text_coverage=0.0, text_lines=0, tags=()):
    block = {"textCoverage": text_coverage, "textLines": text_lines,
             "faces": list(faces)}
    if subject is not None:
        block["subject"] = subject
    return {"tags": list(tags), "cull": block}


def face(box=(0.35, 0.2, 0.3, 0.3), eye_height=0.012, rotation=0.0,
         quality=0.7):
    x, y, width, height = box
    return {
        "box": {"x": x, "y": y, "width": width, "height": height},
        "quality": quality,
        "leftEye": eye_contour(0.045, eye_height,
                               (x + width * 0.3, y + height * 0.38), rotation),
        "rightEye": eye_contour(0.045, eye_height,
                                (x + width * 0.7, y + height * 0.38), rotation),
    }


class EyeGeometryTests(unittest.TestCase):
    """The blink measurement itself, on contours rather than on pixels.

    The corpus this was calibrated against contains no closed eyes, and
    squashing an eye in the pixels produces a smear that Vision fits an
    ordinary contour to. The geometry is therefore tested directly.
    """

    def test_open_eye_scores_well_above_the_blink_threshold(self):
        ratio = culling.eye_aspect_ratio(eye_contour(0.045, 0.016))
        self.assertGreater(ratio, culling.THRESHOLDS["eyesOpenRatio"])

    def test_closed_eye_scores_below_the_blink_threshold(self):
        ratio = culling.eye_aspect_ratio(eye_contour(0.045, 0.003))
        self.assertLess(ratio, culling.THRESHOLDS["eyesOpenRatio"])

    def test_a_tilted_head_does_not_read_as_a_squint(self):
        upright = culling.eye_aspect_ratio(eye_contour(0.045, 0.016))
        tilted = culling.eye_aspect_ratio(
            eye_contour(0.045, 0.016, rotation=0.7))
        self.assertAlmostEqual(upright, tilted, places=6)

    def test_degenerate_contours_report_nothing_rather_than_guessing(self):
        for value in (None, [], [[0.1, 0.1]], "eye", [[0.1, 0.1], [0.2, 0.2]]):
            self.assertIsNone(culling.eye_aspect_ratio(value))


class EyesOpenTests(unittest.TestCase):
    def test_open_eyes_on_a_large_face_are_a_select(self):
        record = culling.analyze(texture(), vision(faces=[face()]))
        self.assertEqual(record["criteria"]["eyesOpen"]["verdict"], "yes")

    def test_closed_eyes_are_not_a_select(self):
        record = culling.analyze(
            texture(), vision(faces=[face(eye_height=0.002)]))
        self.assertEqual(record["criteria"]["eyesOpen"]["verdict"], "no")

    def test_one_blink_in_a_group_loses_the_whole_frame(self):
        record = culling.analyze(texture(), vision(faces=[
            face(box=(0.05, 0.2, 0.3, 0.3)),
            face(box=(0.55, 0.2, 0.3, 0.3), eye_height=0.002),
        ]))
        self.assertEqual(record["criteria"]["eyesOpen"]["verdict"], "no")

    def test_a_face_too_small_to_judge_is_not_answered(self):
        record = culling.analyze(
            texture(), vision(faces=[face(box=(0.4, 0.4, 0.02, 0.02))]))
        self.assertEqual(record["criteria"]["eyesOpen"]["verdict"], "unknown")

    def test_a_photo_with_no_face_is_not_answered(self):
        record = culling.analyze(texture(), vision())
        self.assertEqual(record["criteria"]["eyesOpen"]["verdict"], "unknown")


class ExposureTests(unittest.TestCase):
    def test_a_normally_exposed_frame_is_not_rejected(self):
        record = culling.analyze(texture(), vision())
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "no")

    def test_a_blown_frame_is_rejected(self):
        record = culling.analyze(np.clip(texture() * 4.0, 0, 1), vision())
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "yes")

    def test_a_black_frame_is_rejected(self):
        record = culling.analyze(texture() * 0.02, vision())
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "yes")

    def blown_window(self, subject_region):
        """One frame with a blown window, and a subject placed either clear
        of it or inside it. Same pixels, so only the subject rule differs."""
        image = texture(base=0.42, scale=0.2)
        image[:, 380:, :] = 1.0
        return image, mask_payload(image.shape[:2], subject_region)

    def test_a_blown_background_behind_a_good_subject_is_kept(self):
        """A high-key portrait is a choice. Clipping that never reaches the
        subject is not an exposure fault."""
        image, subject = self.blown_window((0.1, 0.95, 0.0, 0.6))
        record = culling.analyze(image, vision(subject=subject))
        clipped = float((image.mean(axis=2) >= 0.996).mean())
        self.assertGreater(clipped, 0.2)
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "no")

    def test_the_same_clipping_on_the_subject_is_rejected(self):
        image, subject = self.blown_window((0.1, 0.95, 0.78, 1.0))
        record = culling.analyze(image, vision(subject=subject))
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "yes")
        self.assertIn("subject", record["criteria"]["exposure"]["detail"])

    def test_clipping_that_takes_over_the_frame_is_rejected_regardless(self):
        image = texture(base=0.6, scale=0.2)
        image[:, 120:, :] = 1.0
        subject = mask_payload(image.shape[:2], (0.1, 0.95, 0.0, 0.2))
        record = culling.analyze(image, vision(subject=subject))
        self.assertGreater(float((image.mean(axis=2) >= 0.996).mean()),
                           culling.THRESHOLDS["clippedHigh"])
        self.assertEqual(record["criteria"]["exposure"]["verdict"], "yes")


class MisfireTests(unittest.TestCase):
    def test_a_frame_with_detail_is_not_a_misfire(self):
        record = culling.analyze(texture(), vision())
        self.assertEqual(record["criteria"]["misfire"]["verdict"], "no")

    def test_a_frame_with_no_plane_of_focus_is_a_misfire(self):
        record = culling.analyze(blur(texture(), 6.0), vision())
        self.assertEqual(record["criteria"]["misfire"]["verdict"], "yes")

    def test_a_lens_cap_frame_is_a_misfire(self):
        record = culling.analyze(texture() * 0.01, vision())
        self.assertEqual(record["criteria"]["misfire"]["verdict"], "yes")


class SubjectSharpnessTests(unittest.TestCase):
    def region(self):
        return (0.15, 0.85, 0.1, 0.5)

    def framed(self, subject_sigma=0.0, frame_sigma=0.0):
        image = texture(seed=3)
        if frame_sigma:
            image = blur(image, frame_sigma)
        if subject_sigma:
            top, bottom, left, right = self.region()
            height, width = image.shape[:2]
            region = np.zeros((height, width), dtype=np.float32)
            region[int(top * height):int(bottom * height),
                   int(left * width):int(right * width)] = 1.0
            # Feathered, because a hard seam would itself be sharp detail
            # sitting inside the subject and would defeat the measurement.
            feather = ndimage.gaussian_filter(region, 6.0)[..., None]
            image = image * (1 - feather) + blur(image, subject_sigma) * feather
        return image, mask_payload(image.shape[:2], self.region())

    def test_a_sharp_subject_is_a_select(self):
        image, subject = self.framed()
        record = culling.analyze(image, vision(subject=subject))
        self.assertEqual(record["criteria"]["subjectSharpness"]["verdict"], "yes")

    def test_focus_landing_behind_the_subject_is_not_a_select(self):
        image, subject = self.framed(subject_sigma=5.0)
        record = culling.analyze(image, vision(subject=subject))
        self.assertEqual(record["criteria"]["subjectSharpness"]["verdict"], "no")
        self.assertIn("sharper", record["criteria"]["subjectSharpness"]["detail"])

    def test_a_frame_blurred_end_to_end_is_not_a_select(self):
        """The ratio alone cannot see this, because subject and background
        soften together. The absolute floor is what catches it."""
        image, subject = self.framed(frame_sigma=5.0)
        record = culling.analyze(image, vision(subject=subject))
        self.assertEqual(record["criteria"]["subjectSharpness"]["verdict"], "no")
        self.assertIn("no fine detail",
                      record["criteria"]["subjectSharpness"]["detail"])

    def test_no_subject_segmentation_is_not_answered(self):
        record = culling.analyze(texture(), vision())
        self.assertEqual(record["criteria"]["subjectSharpness"]["verdict"],
                         "unknown")


class DocumentTests(unittest.TestCase):
    def test_a_page_of_text_is_rejected(self):
        record = culling.analyze(
            texture(base=0.85, scale=0.08),
            vision(text_coverage=0.14, text_lines=22, tags=["document"]))
        self.assertEqual(record["criteria"]["document"]["verdict"], "yes")

    def test_a_street_scene_with_signage_is_kept(self):
        record = culling.analyze(
            texture(), vision(text_coverage=0.03, text_lines=5,
                              tags=["sign", "street", "building"]))
        self.assertEqual(record["criteria"]["document"]["verdict"], "no")

    def test_a_portrait_holding_printed_matter_is_kept(self):
        """A face this size means a photograph, whatever is written in it."""
        record = culling.analyze(
            texture(),
            vision(faces=[face(box=(0.3, 0.15, 0.28, 0.3))],
                   text_coverage=0.09, text_lines=14, tags=["text", "paper"]))
        self.assertEqual(record["criteria"]["document"]["verdict"], "no")


class SimilaritySignatureTests(unittest.TestCase):
    def test_signature_is_deterministic_and_compact(self):
        image = texture()
        first = culling.analyze(image)["similarity"]
        self.assertEqual(first, culling.analyze(image.copy())["similarity"])
        self.assertEqual(first["version"], 1)
        self.assertEqual(len(bytes.fromhex(first["hash"])), 8)
        self.assertEqual(len(bytes.fromhex(first["layout"])), 48)
        self.assertAlmostEqual(first["aspect"], 512 / 384, places=3)
        self.assertLess(len(json.dumps(first)), 230)

    def test_flat_frames_are_marked_as_low_information(self):
        for level in (0.0, 0.5, 1.0):
            record = culling.analyze(np.full((64, 96, 3), level, np.float32))
            self.assertEqual(record["similarity"]["contrast"], 0.0)
            self.assertEqual(record["similarity"]["hash"], "0000000000000000")

    def test_different_color_scenes_do_not_share_the_complete_signature(self):
        image = np.zeros((64, 96, 3), np.float32)
        image[:, 48:, 0] = 0.8
        other = image[..., [2, 1, 0]]
        self.assertNotEqual(culling.analyze(image)["similarity"]["layout"],
                            culling.analyze(other)["similarity"]["layout"])


class RecordShapeTests(unittest.TestCase):
    def test_every_criterion_is_answered_with_a_reason(self):
        record = culling.analyze(texture(), vision(faces=[face()]))
        self.assertEqual(record["version"], culling.ANALYSIS_VERSION)
        self.assertEqual(set(record["criteria"]), set(culling.CRITERIA))
        for name, entry in record["criteria"].items():
            self.assertIn(entry["verdict"], (culling.YES, culling.NO,
                                             culling.UNKNOWN), name)
            self.assertTrue(entry["detail"], name)

    def test_without_vision_cues_only_the_pixel_criteria_are_answered(self):
        """Exposure and misfires need no Vision. The rest must say so rather
        than answering no, which would read as a judgement."""
        record = culling.analyze(texture(), None)
        criteria = record["criteria"]
        self.assertEqual(criteria["subjectSharpness"]["verdict"], "unknown")
        self.assertEqual(criteria["eyeSharpness"]["verdict"], "unknown")
        self.assertEqual(criteria["eyesOpen"]["verdict"], "unknown")
        self.assertEqual(criteria["exposure"]["verdict"], "no")
        self.assertEqual(criteria["misfire"]["verdict"], "no")

    def test_matches_only_counts_a_yes(self):
        record = culling.analyze(np.clip(texture() * 4.0, 0, 1), vision())
        self.assertTrue(culling.matches(record, ["exposure"]))
        self.assertFalse(culling.matches(record, ["document"]))
        self.assertFalse(culling.matches(record, ["subjectSharpness"]))
        self.assertFalse(culling.matches(None, ["exposure"]))
        self.assertFalse(culling.matches({}, culling.CRITERIA))

    def test_a_damaged_mask_is_ignored_rather_than_raising(self):
        for broken in ({"edge": 64, "mask": "not base64"},
                       {"edge": 64, "mask": base64.b64encode(b"short").decode()},
                       {"edge": 0, "mask": ""}, {"mask": None}):
            record = culling.analyze(texture(), vision(subject=broken))
            self.assertEqual(record["criteria"]["subjectSharpness"]["verdict"],
                             "unknown")


class CullStorageTests(unittest.TestCase):
    def test_verdicts_survive_a_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = IndexStore(Path(temporary) / "index.sqlite3")
            record = culling.analyze(texture(), vision(faces=[face()]))
            store.upsert("a.jpg", "v1", {"tags": ["x"], "cull": record},
                         time.time(), culling.ANALYSIS_VERSION)
            stored = store.results(["a.jpg"])["a.jpg"]["cull"]
            self.assertEqual(stored["criteria"]["eyesOpen"]["verdict"], "yes")

    def test_raising_the_analysis_version_makes_stored_results_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = IndexStore(Path(temporary) / "index.sqlite3")
            store.upsert("a.jpg", "v1", {"cull": {}}, time.time(),
                         culling.ANALYSIS_VERSION)
            self.assertTrue(store.is_current("a.jpg", "v1",
                                             culling.ANALYSIS_VERSION))
            self.assertFalse(store.is_current("a.jpg", "v1",
                                              culling.ANALYSIS_VERSION + 1))
            self.assertFalse(store.is_current("a.jpg", "v2",
                                              culling.ANALYSIS_VERSION))

    def test_an_index_written_before_culling_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE photos (
                    name TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    ocr_json TEXT NOT NULL DEFAULT '[]',
                    faces_json TEXT NOT NULL DEFAULT '[]',
                    provider TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE VIRTUAL TABLE photo_search USING fts5(
                    name UNINDEXED, caption, tags, ocr,
                    tokenize='unicode61 remove_diacritics 2');
                """)
            legacy.execute(
                "INSERT INTO photos (name, fingerprint, caption, tags_json,"
                " updated_at) VALUES ('old.jpg', 'v1', 'a bicycle',"
                " '[\"bicycle\"]', 1.0)")
            legacy.commit(); legacy.close()

            store = IndexStore(path)
            results = store.results(["old.jpg"])
            self.assertEqual(results["old.jpg"]["tags"], ["bicycle"])
            self.assertEqual(results["old.jpg"]["cull"], {})
            # The photo keeps its searchable metadata but is scored again,
            # because it predates the culling pass.
            self.assertFalse(store.is_current("old.jpg", "v1",
                                              culling.ANALYSIS_VERSION))


if __name__ == "__main__":
    unittest.main()
