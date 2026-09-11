#!/usr/bin/env python3
"""Package and publish LightTable to Chocolatey."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
import zipfile


PUSH_URL = "https://push.chocolatey.org/"


def load_api_key_from_env_file() -> str | None:
    env_file = Path.home() / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("CHOCOLATEY_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def pack_nupkg(source_dir: Path, output_file: Path) -> None:
    nuspec_file = source_dir / "lighttable.nuspec"
    if not nuspec_file.is_file():
        raise FileNotFoundError(f"Missing nuspec file: {nuspec_file}")

    rels = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.microsoft.com/packaging/2010/07/manifest" Target="/lighttable.nuspec" Id="R1" />
</Relationships>"""

    content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />
  <Default Extension="ps1" ContentType="application/octet" />
  <Default Extension="nuspec" ContentType="application/octet" />
</Types>"""

    dt = (2026, 9, 10, 0, 0, 0)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zf:
        def add_bytes(arcname: str, data: bytes):
            zi = zipfile.ZipInfo(arcname, dt)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)

        add_bytes("_rels/.rels", rels.encode("utf-8"))
        add_bytes("[Content_Types].xml", content_types.encode("utf-8"))
        add_bytes("lighttable.nuspec", nuspec_file.read_bytes())
        for script in ["chocolateyinstall.ps1", "chocolateyuninstall.ps1"]:
            script_path = source_dir / "tools" / script
            if script_path.is_file():
                add_bytes(f"tools/{script}", script_path.read_bytes())

    print(f"Packaged {output_file} ({output_file.stat().st_size} bytes)")


def push_nupkg(package_path: Path, api_key: str) -> None:
    boundary = "----WebKitFormBoundaryChocoPush" + os.urandom(8).hex()
    nupkg_bytes = package_path.read_bytes()

    body = (
        f"--{boundary}\r\n".encode("ascii")
        + f'Content-Disposition: form-data; name="package"; filename="{package_path.name}"\r\n'.encode("ascii")
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + nupkg_bytes
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )

    req = urllib.request.Request(
        PUSH_URL,
        data=body,
        headers={
            "X-NuGet-ApiKey": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Chocolatey command line/2.2.2",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            if '"isAuthenticated":false' in content:
                print("Error: Chocolatey API rejected the key (isAuthenticated: false).", file=sys.stderr)
                sys.exit(1)
            print(f"Successfully pushed {package_path.name} to Chocolatey! Response: {resp.status}")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        print(f"Push failed (HTTP {e.code}): {err_msg}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Package and push LightTable to Chocolatey")
    parser.add_argument("--package", type=Path, help="Path to existing .nupkg")
    parser.add_argument("--source-dir", type=Path, default=Path("chocolatey/lighttable"), help="Source folder with lighttable.nuspec")
    parser.add_argument("--api-key", help="Chocolatey API key (or set CHOCOLATEY_API_KEY)")
    parser.add_argument("--pack-only", action="store_true", help="Only build .nupkg, do not push")
    args = parser.parse_args()

    nupkg_path = args.package
    if not nupkg_path or not nupkg_path.is_file():
        if args.source_dir.is_dir() and (args.source_dir / "lighttable.nuspec").is_file():
            nuspec_text = (args.source_dir / "lighttable.nuspec").read_text(encoding="utf-8")
            match = re.search(r"<version>(.*?)</version>", nuspec_text)
            ver = match.group(1) if match else "0.0.0"
            nupkg_path = Path(f"lighttable.{ver}.nupkg")
            pack_nupkg(args.source_dir, nupkg_path)
        else:
            print(f"Error: package {nupkg_path} not found and source-dir {args.source_dir} is missing nuspec.", file=sys.stderr)
            sys.exit(1)

    if args.pack_only:
        return

    key = args.api_key or os.environ.get("CHOCOLATEY_API_KEY") or load_api_key_from_env_file()
    if not key:
        print("Error: Chocolatey API key not found in --api-key, CHOCOLATEY_API_KEY, or ~/.env", file=sys.stderr)
        sys.exit(1)

    push_nupkg(nupkg_path, key)


if __name__ == "__main__":
    main()
