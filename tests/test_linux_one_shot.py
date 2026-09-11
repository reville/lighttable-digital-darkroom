# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np
import tifffile

import film_pipeline
import render_cli


class OneShotFallbackTests(unittest.TestCase):
    def test_fallback_selects_linux_cpu_without_changing_other_platforms_or_pixels(self):
        pixels = np.array([[[32768, 32769, 32770], [8191, 8192, 8193]]], dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            engine = app / "engine"
            engine.mkdir()
            (engine / "spektrafilm-rs").touch()
            source = app / "input.tif"
            tifffile.imwrite(source, pixels, photometric="rgb")

            for platform in ("linux", "darwin", "win32"):
                with self.subTest(platform=platform):
                    def process(command, **options):
                        if platform == "linux":
                            self.assertEqual(options["env"]["SPEKTRAFILM_BACKEND"], "cpu")
                            self.assertEqual(options["env"]["LIGHTTABLE_TEST_VALUE"], "preserved")
                        else:
                            self.assertIsNone(options["env"])
                        # The upstream CLI returns a full-precision RGB16 TIFF.
                        # Exercise the real decoder after checking process routing.
                        tifffile.imwrite(command[command.index("-o") + 1], pixels, photometric="rgb")
                        return subprocess.CompletedProcess(command, 0, "", "")

                    with mock.patch.object(film_pipeline.sys, "platform", platform), \
                            mock.patch.object(render_cli, "__file__", str(app / "render_cli.py")), \
                            mock.patch.dict(os.environ, {"SPEKTRAFILM_BACKEND": "wgpu",
                                                         "LIGHTTABLE_TEST_VALUE": "preserved"}), \
                            mock.patch.object(render_cli.subprocess, "run", side_effect=process):
                        result = render_cli.render_rust(str(source), {})
                        self.assertEqual(os.environ["SPEKTRAFILM_BACKEND"], "wgpu")
                    np.testing.assert_array_equal(result, pixels.astype(np.float32) / 65535.0)


if __name__ == "__main__":
    unittest.main()
