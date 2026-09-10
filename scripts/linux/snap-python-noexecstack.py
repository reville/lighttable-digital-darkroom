#!/usr/bin/env python3
"""Clear the upstream CPython 3.13.12 GNU_STACK flag in the Snap staging tree.

This changes only PF_X in one ELF program header of the exact published library.
It leaves the public archive and application source identity untouched. See
https://github.com/astral-sh/python-build-standalone/issues/1072.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

LIBRARY = "Python/lib/libpython3.13.so.1.0"
SOURCE_REVISION = "be537f2f3e2e431ae6b42af716c2a8b365f57bab"
BEFORE_SHA256 = "9abada6f75bdbf9d401232a6203a6c26a867a9b5314173e802523c8e3de03d32"
AFTER_SHA256 = "ab3e81f302cf83918e7f03acadc8265f7add416e7de5a30c66e04301448da39a"
UPSTREAM_ISSUE = "https://github.com/astral-sh/python-build-standalone/issues/1072"
PROVENANCE = "snap-runtime-adjustments.json"


def stack_header(data: bytes) -> tuple[int, int]:
    """Return the flags offset/value for one unambiguous ELF64 GNU_STACK header."""
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("Expected a complete little-endian ELF64 library")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    _, elf_type, machine, version, _, phoff, _, _, ehsize, phsize, phnum, _, _, _ = header
    if (elf_type != 3 or machine != 62 or version != 1 or ehsize != 64
            or phsize != 56 or not 1 <= phnum <= 128 or phoff < 64
            or phoff % 8 or phoff + phnum * phsize > len(data)):
        raise ValueError("Unexpected x86_64 shared-library program header layout")
    found = []
    for index in range(phnum):
        offset = phoff + index * phsize
        kind, flags, file_offset, address, physical, file_size, memory_size, alignment = struct.unpack_from(
            "<IIQQQQQQ", data, offset)
        if kind == 0x6474E551:  # PT_GNU_STACK
            if (flags not in (6, 7) or any((file_offset, address, physical, file_size, memory_size))
                    or alignment != 16):
                raise ValueError("Unexpected GNU_STACK header contents")
            found.append((offset + 4, flags))
    if len(found) != 1:
        raise ValueError("Expected exactly one GNU_STACK program header")
    return found[0]


def library_bytes(bundle: Path) -> tuple[Path, bytes]:
    path = bundle / LIBRARY
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bundled libpython must be a regular file")
    return path, path.read_bytes()


def clear_execstack(bundle: Path) -> dict:
    """Patch only the known upstream binary; reject unknown or already modified input."""
    manifest = json.loads((bundle / "build-manifest.json").read_text())
    if (manifest.get("source_revision") != SOURCE_REVISION or manifest.get("version") != "0.5.0"
            or manifest.get("source_dirty") is not False or manifest.get("platform") != "linux"
            or manifest.get("architecture") != "x86_64"):
        raise ValueError("GNU_STACK workaround requires the exact immutable Linux 0.5.0 release")
    path, before = library_bytes(bundle)
    if hashlib.sha256(before).hexdigest() != BEFORE_SHA256:
        raise ValueError("Bundled libpython differs from the pinned upstream binary")
    offset, flags = stack_header(before)
    if flags != 7:
        raise ValueError("Pinned libpython no longer has the expected executable-stack flag")
    after = bytearray(before)
    struct.pack_into("<I", after, offset, flags & ~1)  # Clear PF_X only.
    if hashlib.sha256(after).hexdigest() != AFTER_SHA256:
        raise ValueError("GNU_STACK adjustment differs from the reviewed output binary")
    # Staging-tree only: preserve file mode and all bytes outside p_flags.
    with path.open("r+b") as stream:
        stream.seek(offset)
        stream.write(after[offset:offset + 4])
    if path.read_bytes() != after:
        raise ValueError("Bundled libpython changed while applying the GNU_STACK adjustment")
    record = {"format": 1, "source_revision": SOURCE_REVISION,
              "upstream_issue": UPSTREAM_ISSUE, "path": LIBRARY,
              "operation": "Clear PF_X in the existing PT_GNU_STACK program header",
              "flags_offset": offset, "flags_before": flags, "flags_after": flags & ~1,
              "sha256_before": BEFORE_SHA256, "sha256_after": AFTER_SHA256,
              "changed_byte_count": sum(a != b for a, b in zip(before, after))}
    (bundle / PROVENANCE).write_text(json.dumps(record, indent=2) + "\n")
    return record


def verify(bundle: Path) -> dict:
    """Read-only check of the installed library, its non-executable stack and receipt."""
    _, data = library_bytes(bundle)
    offset, flags = stack_header(data)
    if flags != 6 or hashlib.sha256(data).hexdigest() != AFTER_SHA256:
        raise ValueError("Installed libpython is not the verified non-executable-stack binary")
    record = json.loads((bundle / PROVENANCE).read_text())
    if (record.get("source_revision") != SOURCE_REVISION or record.get("path") != LIBRARY
            or record.get("sha256_before") != BEFORE_SHA256 or record.get("sha256_after") != AFTER_SHA256
            or record.get("flags_offset") != offset or record.get("flags_before") != 7
            or record.get("flags_after") != 6 or record.get("changed_byte_count") != 1):
        raise ValueError("Installed GNU_STACK adjustment provenance does not match")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--verify", action="store_true", help="Verify an existing adjustment without modifying files")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.bundle) if args.verify else clear_execstack(args.bundle), indent=2))
    except (OSError, ValueError, KeyError, struct.error) as error:
        parser.exit(1, f"LightTable: {error}\n")


if __name__ == "__main__":
    main()
