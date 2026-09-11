# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("snap_python_noexecstack", ROOT / "scripts/linux/snap-python-noexecstack.py")
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)


def library_fixture() -> bytes:
    """A valid ELF64 shared-library header with one LOAD and one GNU_STACK entry."""
    identity = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = struct.pack("<16sHHIQQQIHHHHHH", identity, 3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 176, 176, 4096)
    gnu_stack = struct.pack("<IIQQQQQQ", 0x6474E551, 7, 0, 0, 0, 0, 0, 16)
    return header + load + gnu_stack


def fixture_hashes():
    before = library_fixture()
    after = bytearray(before)
    struct.pack_into("<I", after, 124, 6)
    return {"BEFORE_SHA256": hashlib.sha256(before).hexdigest(),
            "AFTER_SHA256": hashlib.sha256(after).hexdigest()}


class SnapPythonNoExecStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name) / "LightTable"
        self.library = self.bundle / stack.LIBRARY
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(library_fixture())
        self.library.chmod(0o755)
        (self.bundle / "build-manifest.json").write_text(json.dumps({
            "version": "0.5.0", "source_revision": stack.SOURCE_REVISION,
            "source_dirty": False, "platform": "linux", "architecture": "x86_64"}))
        self.patch = patch.multiple(stack, **fixture_hashes())
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_clears_exactly_one_flag_byte_preserving_mode_and_source_identity(self):
        before = self.library.read_bytes()
        manifest = (self.bundle / "build-manifest.json").read_bytes()
        receipt = stack.clear_execstack(self.bundle)
        after = self.library.read_bytes()
        self.assertEqual([i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]], [124])
        self.assertEqual(after[124], 6)
        self.assertEqual(self.library.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.bundle / "build-manifest.json").read_bytes(), manifest)
        self.assertEqual(stack.verify(self.bundle), receipt)
        with self.assertRaisesRegex(ValueError, "pinned upstream binary"):
            stack.clear_execstack(self.bundle)
        self.assertEqual(self.library.read_bytes(), after)

    def test_unknown_input_and_output_hashes_fail_without_writing(self):
        before = self.library.read_bytes()
        for field in ("BEFORE_SHA256", "AFTER_SHA256"):
            with patch.object(stack, field, "0" * 64):
                with self.assertRaises(ValueError):
                    stack.clear_execstack(self.bundle)
            self.assertEqual(self.library.read_bytes(), before)
            self.assertFalse((self.bundle / stack.PROVENANCE).exists())

    def test_rejects_missing_duplicate_truncated_or_wrong_machine_headers(self):
        valid = library_fixture()
        variants = [valid[:63], valid[:-1]]
        for offset, fmt, value in ((18, "<H", 183), (54, "<H", 55), (32, "<Q", 2**32),
                                   (120, "<I", 1), (64, "<I", 0x6474E551)):
            changed = bytearray(valid)
            struct.pack_into(fmt, changed, offset, value)
            variants.append(bytes(changed))
        for invalid in variants:
            with self.subTest(length=len(invalid), data=invalid[:24]):
                with self.assertRaises(ValueError):
                    stack.stack_header(invalid)

    def test_verification_rejects_reintroduced_execstack(self):
        stack.clear_execstack(self.bundle)
        self.library.write_bytes(library_fixture())
        with self.assertRaisesRegex(ValueError, "non-executable-stack"):
            stack.verify(self.bundle)


if __name__ == "__main__":
    unittest.main()
