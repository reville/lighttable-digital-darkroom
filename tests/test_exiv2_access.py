# SPDX-License-Identifier: GPL-3.0-only
"""python-exiv2 must be set up once, before any thread reads metadata.

libexiv2 0.28 initializes its XMP toolkit on the first XMP read without a
lock. Two request threads reading their first photos together crashed the
rendering engine with SIGSEGV (a reported 0.7.6 crash) or left it spinning.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import exiv2_access

ROOT = Path(__file__).resolve().parents[1]
# Standalone tooling may import the binding directly; the app may not.
EXEMPT = {"tests", "bench", "scripts", "vendor", "build", "packaging", ".venv", "cache"}

HAVE_EXIV2 = importlib.util.find_spec("exiv2") is not None

RACE_CHILD = r"""
import sys, threading
sys.path.insert(0, sys.argv[1])
import platform_image, xmp_sidecar
paths = sys.argv[2:]
barrier = threading.Barrier(len(paths))

def read(index, path):
    barrier.wait()
    if index % 2:
        platform_image.metadata(path, force_portable=True)
    else:
        xmp_sidecar.read_embedded(path)

threads = [threading.Thread(target=read, args=item) for item in enumerate(paths)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
"""


class Exiv2ImportContractTests(unittest.TestCase):
    def test_application_modules_reach_exiv2_only_through_the_loader(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            relative = path.relative_to(ROOT)
            if relative.parts[0] in EXEMPT or path.name == "exiv2_access.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(relative))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(name == "exiv2" or name.startswith("exiv2.") for name in names):
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [], "import exiv2 through exiv2_access.load()")


class Exiv2LoaderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        initialized = threading.Event()

        def initialize():
            self.calls.append("initialize")
            time.sleep(0.05)  # widen the window a racing caller would use
            initialized.set()

        def register(uri, prefix):
            self.assertTrue(initialized.is_set(), "namespaces need the toolkit first")
            self.calls.append(("register", prefix))

        self.initialized = initialized
        self.fake = types.SimpleNamespace(
            XmpParser=types.SimpleNamespace(initialize=initialize),
            XmpProperties=types.SimpleNamespace(registerNs=register))
        patcher = mock.patch.dict(sys.modules, {"exiv2": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        state = mock.patch.object(exiv2_access, "_binding", None)
        state.start()
        self.addCleanup(state.stop)

    def test_concurrent_first_use_initializes_once_before_any_caller_returns(self):
        barrier = threading.Barrier(16)
        seen = []

        def caller():
            barrier.wait()
            module = exiv2_access.load()
            seen.append((module, self.initialized.is_set()))

        threads = [threading.Thread(target=caller) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(self.calls, ["initialize", ("register", "lr")])
        self.assertEqual(len(seen), 16)
        self.assertTrue(all(module is self.fake and ready for module, ready in seen))
        self.assertIs(exiv2_access.load(), self.fake)
        self.assertEqual(len(self.calls), 2)

    def test_a_predefined_namespace_does_not_block_setup(self):
        def refuse(uri, prefix):
            raise RuntimeError("already registered")

        self.fake.XmpProperties.registerNs = refuse
        self.assertIs(exiv2_access.load(), self.fake)
        self.assertEqual(self.calls, ["initialize"])


@unittest.skipUnless(HAVE_EXIV2, "python-exiv2 is not installed")
class Exiv2FirstReadRaceTests(unittest.TestCase):
    """Fresh processes, each starting four metadata reads at once."""

    PROCESSES = 32
    BATCH = 4

    def test_parallel_first_reads_neither_crash_nor_hang(self):
        from PIL import Image

        binding = exiv2_access.load()
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for index in range(4):
                path = Path(folder) / f"xmp-{index}.jpg"
                Image.new("RGB", (32, 24), (index * 40, 90, 160)).save(path, "JPEG")
                image = binding.ImageFactory.open(str(path))
                image.readMetadata()
                image.xmpData()["Xmp.dc.title"] = f"fixture {index}"
                image.exifData()["Exif.Image.Model"] = "Fixture"
                image.writeMetadata()
                paths.append(str(path))
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            failures = []
            for _ in range(0, self.PROCESSES, self.BATCH):
                batch = [subprocess.Popen(
                    [sys.executable, "-c", RACE_CHILD, str(ROOT), *paths],
                    cwd=ROOT, env=environment, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE) for _ in range(self.BATCH)]
                # A healthy batch takes well under a second; a hung one
                # must not hold the suite for a timeout per child.
                deadline = time.monotonic() + 20
                for child in batch:
                    try:
                        _, error = child.communicate(
                            timeout=max(0.1, deadline - time.monotonic()))
                        if child.returncode:
                            failures.append((child.returncode,
                                             error.decode(errors="replace")[-400:]))
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate()
                        failures.append(("hang", ""))
                if failures:
                    break
            self.assertEqual(failures, [], "a first-read race crashed or hung")


if __name__ == "__main__":
    unittest.main()
