# SPDX-License-Identifier: GPL-3.0-only
import unittest

import merge_plan


class MergePlanTests(unittest.TestCase):
    def test_default_plan_is_full_resolution_when_memory_is_plentiful(self):
        dims = [(6000, 4000)] * 4
        plan = merge_plan.plan_input_edge(
            "panorama", dims, available=64 * 1024 ** 3, hard_cap=None)
        self.assertFalse(plan["limited"])
        self.assertIsNone(plan["edge"])
        self.assertEqual(plan["effectiveEdge"], 6000)
        self.assertIsNone(plan["notice"])

    def test_low_memory_reduces_the_edge_with_a_notice(self):
        dims = [(9000, 6000)] * 6
        plan = merge_plan.plan_input_edge(
            "hdr", dims, available=200 * 1024 ** 2, hard_cap=None)
        self.assertTrue(plan["limited"])
        self.assertEqual(plan["reason"], "memory")
        self.assertLess(plan["edge"], 9000)
        self.assertIsNotNone(plan["notice"])

    def test_explicit_env_style_hard_cap_always_applies(self):
        dims = [(9000, 6000)]
        plan = merge_plan.plan_input_edge(
            "focus", dims, available=64 * 1024 ** 3, hard_cap=4000)
        self.assertTrue(plan["limited"])
        self.assertEqual(plan["reason"], "cap")
        self.assertEqual(plan["edge"], 4000)

    def test_hard_cap_larger_than_the_inputs_has_no_effect(self):
        dims = [(2000, 1500)]
        plan = merge_plan.plan_input_edge(
            "panorama", dims, available=64 * 1024 ** 3, hard_cap=6000)
        self.assertFalse(plan["limited"])
        self.assertEqual(plan["effectiveEdge"], 2000)

    def test_unknown_available_memory_does_not_crash_and_stays_full_size(self):
        plan = merge_plan.plan_input_edge(
            "focus", [(4000, 3000)], available=None, hard_cap=None)
        self.assertIn("effectiveEdge", plan)

    def test_focus_mode_uses_a_smaller_multiplier_than_panorama(self):
        dims = [(8000, 6000)] * 8
        available = 3 * 1024 ** 3
        focus = merge_plan.plan_input_edge("focus", dims, available=available, hard_cap=None)
        panorama = merge_plan.plan_input_edge("panorama", dims, available=available, hard_cap=None)
        self.assertGreaterEqual(focus["effectiveEdge"], panorama["effectiveEdge"])

    def test_estimate_bytes_scales_with_pixel_count(self):
        small = merge_plan.estimate_bytes("hdr", [(1000, 1000)], None)
        large = merge_plan.estimate_bytes("hdr", [(2000, 2000)], None)
        self.assertAlmostEqual(large / small, 4.0, places=3)


if __name__ == "__main__":
    unittest.main()
