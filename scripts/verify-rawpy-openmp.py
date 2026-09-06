#!/usr/bin/env python3
"""Fail packaging if OpenMP is missing or a dylib still needs the build host."""
from pathlib import Path
import re
import subprocess
import rawpy
import rawpy_openmp

assert rawpy.__version__ == "0.26.1", rawpy.__version__
assert rawpy_openmp.__version__ == "0.26.1", rawpy_openmp.__version__
assert rawpy_openmp.flags.get("OPENMP"), rawpy_openmp.flags
assert hasattr(rawpy_openmp.RawPy, "is_xtrans"), "missing header-only sensor dispatch"
package = Path(rawpy_openmp.__file__).resolve().parent
libraries = list(package.rglob("*.dylib")) + list(package.glob("*.so"))
assert any("libomp" in p.name for p in libraries), "OpenMP runtime not bundled"
for path in libraries:
    links = subprocess.check_output(["otool", "-L", str(path)], text=True)
    # Delocate uses /DLC/... for a dylib's own LC_ID_DYLIB. It is an
    # identifier, not a dependency; consumers use @loader_path references.
    ids = subprocess.check_output(["otool", "-D", str(path)], text=True).splitlines()[1:]
    for line in links.splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        if dependency in ids:
            continue
        assert dependency.startswith(("@loader_path/", "@rpath/", "/usr/lib/",
                                      "/System/Library/")), (path.name, dependency)
        if dependency.startswith("@loader_path/"):
            target = (path.parent / dependency.removeprefix("@loader_path/")).resolve()
            assert target.is_relative_to(package) and target.is_file(), (path.name, dependency)
    commands = subprocess.check_output(["otool", "-l", str(path)], text=True)
    minimum = re.search(r"\bminos (\d+)\.(\d+)", commands)
    if minimum is None:
        minimum = re.search(r"LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+)\.(\d+)", commands)
    assert minimum is not None, (path.name, "missing macOS deployment target")
    assert tuple(map(int, minimum.groups())) <= (13, 0), (path.name, minimum.group(0))
print(f"rawpy {rawpy.__version__} retained; LibRaw {rawpy_openmp.libraw_version}, OpenMP bundled")
