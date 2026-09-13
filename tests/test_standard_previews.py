# SPDX-License-Identifier: GPL-3.0-only
"""Import-time standard previews: the accurate 1100 px input the editor opens with."""
import unittest
from unittest import mock

import server


class StandardPreviewWarmupTests(unittest.TestCase):
    def warm(self, name, *, film=True, ready=False, busy=False):
        calls = []
        state = {"params": {"profile_enabled": film, "rotate": 90}}
        with (
            mock.patch.object(server, "_standard_preview_busy", return_value=busy),
            mock.patch.object(server, "guard_photo"),
            mock.patch.object(server, "guard_local_photo"),
            mock.patch.object(server, "catalog_handle", return_value=None),
            mock.patch.object(server, "edited_thumbnail_state", return_value=state),
            mock.patch.object(server, "standard_preview_ready", return_value=ready),
            mock.patch.object(server, "build_raw_preview",
                              side_effect=lambda *a: calls.append(("raw", a))),
            mock.patch.object(server, "build_neutral_preview",
                              side_effect=lambda *a: calls.append(("neutral", a))),
        ):
            result = server._warm_standard_preview(name)
        return result, calls

    def test_raw_film_photo_gets_its_accurate_1100_input(self):
        result, calls = self.warm("1:photo.raf")
        self.assertTrue(result)
        self.assertEqual(calls, [("raw", ("1:photo.raf", 1100, "full",
                                          {"profile_enabled": True, "rotate": 90}))])

    def test_develop_mode_photo_gets_its_neutral_preview(self):
        result, calls = self.warm("1:photo.raf", film=False)
        self.assertTrue(result)
        self.assertEqual(calls[0][0], "neutral")
        self.assertEqual(calls[0][1][1:3], (1100, 90))

    def test_jpeg_and_ready_photos_are_skipped_and_busy_asks_for_a_retry(self):
        self.assertEqual(self.warm("1:photo.jpg"), (True, []))
        self.assertEqual(self.warm("1:photo.raf", ready=True), (True, []))
        self.assertEqual(self.warm("1:photo.raf", busy=True), (False, []))

    def test_warmup_decodes_at_lowest_priority_without_retaining_pixels(self):
        import raw_decode_runtime
        seen = {}
        def build(*_):
            seen["priority"] = raw_decode_runtime._priority.get()
            seen["retain"] = raw_decode_runtime.retain_pixels()
        with (
            mock.patch.object(server, "_standard_preview_busy", return_value=False),
            mock.patch.object(server, "guard_photo"),
            mock.patch.object(server, "guard_local_photo"),
            mock.patch.object(server, "catalog_handle", return_value=None),
            mock.patch.object(server, "edited_thumbnail_state",
                              return_value={"params": {"profile_enabled": True, "rotate": 0}}),
            mock.patch.object(server, "standard_preview_ready", return_value=False),
            mock.patch.object(server, "build_raw_preview", side_effect=build),
        ):
            server._warm_standard_preview("1:photo.raf")
        self.assertEqual(seen, {"priority": "prefetch", "retain": False})
        self.assertTrue(raw_decode_runtime.retain_pixels())

    def test_scan_queues_the_source_thumbnail_and_the_standard_preview(self):
        with (
            mock.patch.object(server.THUMB_WARMUP, "enqueue", return_value=True) as thumbs,
            mock.patch.object(server.PREVIEW_WARMUP, "enqueue", return_value=True) as previews,
        ):
            self.assertTrue(server._enqueue_scan_warmups(3, "a/b.raf"))
        thumbs.assert_called_once_with(3, "a/b.raf")
        previews.assert_called_once_with(3, "a/b.raf")

    def test_readiness_checks_the_input_the_editor_will_request(self):
        params = {"profile_enabled": True, "rotate": 0}
        with (
            mock.patch.object(server, "is_raw", return_value=True),
            mock.patch.object(server, "raw_preview_path") as path,
            mock.patch.object(server, "valid_tiff_cache", return_value=True) as valid,
        ):
            self.assertTrue(server.standard_preview_ready("1:p.raf", {"params": params}))
        path.assert_called_once_with("1:p.raf", 1100, "full", params)
        valid.assert_called_once()
        self.assertTrue(server.standard_preview_ready("1:p.jpg"))

    def test_busy_yields_to_interaction_refinement_and_a_running_decode(self):
        import raw_decode_runtime
        with mock.patch.object(server.RENDER_LOCK, "locked", return_value=False), \
                mock.patch.dict(server.RAW_REFINE_JOBS, {}, clear=True):
            self.assertFalse(server._standard_preview_busy())
            with mock.patch.object(raw_decode_runtime, "decoder_busy", return_value=True):
                self.assertTrue(server._standard_preview_busy())
            server.RAW_REFINE_JOBS["job"] = object()
            self.assertTrue(server._standard_preview_busy())
        with mock.patch.object(server.RENDER_LOCK, "locked", return_value=True):
            self.assertTrue(server._standard_preview_busy())


class PreparedOnlyPrefetchTests(unittest.TestCase):
    def test_accurate_input_ready_checks_the_requested_width_and_mode(self):
        film = {"profile_enabled": True, "rotate": 90}
        with (
            mock.patch.object(server, "is_raw", return_value=True),
            mock.patch.object(server, "raw_preview_path") as raw_path,
            mock.patch.object(server, "valid_tiff_cache", return_value=False),
            mock.patch.object(server, "neutral_preview_path") as neutral_path,
        ):
            neutral_path.return_value.exists.return_value = True
            self.assertFalse(server.accurate_input_ready("1:p.raf", 1100, film))
            self.assertEqual(raw_path.call_args.args[:3], ("1:p.raf", 1100, "full"))
            self.assertTrue(server.accurate_input_ready(
                "1:p.raf", 1100, {"profile_enabled": False, "rotate": 90}))
            self.assertEqual(neutral_path.call_args.args[:3], ("1:p.raf", 1100, 90))
        self.assertTrue(server.accurate_input_ready("1:p.jpg", 1100, film))

    def test_prepared_only_prefetch_is_refused_before_any_render_work(self):
        for ready, rendered in ((False, 0), (True, 1)):
            handler = server.Handler.__new__(server.Handler)
            handler.path = "/api/render"
            handler.headers = {}
            handler._enforce_security = mock.Mock()
            handler._json = mock.Mock()
            handler._log_request = mock.Mock()
            handler._body = lambda: dict(name="1:p.raf", w=1100, priority="prefetch",
                                         prepared_only=True, params={})
            with (
                mock.patch.object(server, "accurate_input_ready", return_value=ready),
                mock.patch.object(server, "render_preview", return_value={}) as render,
                mock.patch.object(server, "apply_preview_edits", return_value={}),
            ):
                handler.do_POST()
            self.assertEqual(render.call_count, rendered)
            if not ready:
                self.assertEqual(handler._json.call_args.args[0]["cancelled"], True)


if __name__ == "__main__":
    unittest.main()
