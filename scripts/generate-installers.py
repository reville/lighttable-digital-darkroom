#!/usr/bin/env python3
"""Generate installer manifests from completed release files, using only stdlib."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile


REPOSITORY = "reville/lighttable-digital-darkroom"
HOMEPAGE = f"https://github.com/{REPOSITORY}"
PACKAGE_ID = "NicholasReville.LightTable"
PUBLISHER = "Nicholas Reville"
DESCRIPTION = "Digital darkroom for RAW photography and film simulation"
MANIFEST_VERSION = "1.12.0"
CHANNEL_ARTIFACTS = {
    "homebrew": "LightTable-{version}-macos-arm64.dmg",
    "scoop": "LightTable-{version}-windows-x64.zip",
    "winget": "LightTable-{version}-windows-x64-setup.exe",
    "chocolatey": "LightTable-{version}-windows-x64-setup.exe",
    "aur": "LightTable-{version}-linux-x86_64.tar.gz",
}


def release_version(value: str) -> str:
    """Use the stable SemVer subset shared by the app and Windows packaging."""
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("version must be a stable semantic version such as 1.2.3 (without v)")
    if any(len(part) > 5 or int(part) > 65535 for part in value.split(".")):
        raise ValueError("version components must be at most 65535 for Windows version resources")
    return value


def validate_license(license_name: str | None, license_url: str | None) -> None:
    if license_name is not None and (
        not license_name.strip() or len(license_name) > 512
        or any(ord(char) < 32 for char in license_name)
    ):
        raise ValueError("license must be a nonempty, single-line license name or identifier")
    if license_url is not None:
        parsed = urlsplit(license_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or any(char.isspace() for char in license_url)):
            raise ValueError("license-url must be a public HTTPS URL without credentials")


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"release artifact must be a regular file: {path.name}")
    before = path.stat()
    if before.st_size == 0:
        raise ValueError(f"release artifact is empty: {path.name}")
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ValueError(f"release artifact changed while hashing: {path.name}")
    return result.hexdigest()


def validate_portable_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {name.replace("\\", "/") for name in archive.namelist()}
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid portable ZIP: {path.name}") from exc
    required = {"LightTable/LightTable.exe", "LightTable/lighttable.cmd"}
    if not required <= names:
        raise ValueError("portable ZIP must contain LightTable/LightTable.exe and LightTable/lighttable.cmd")


def homebrew(version: str, url: str, sha256: str) -> dict[str, str]:
    return {"homebrew/Casks/lighttable.rb": f'''cask "lighttable" do
  version "{version}"
  sha256 "{sha256}"

  url "{url}"
  name "LightTable"
  desc "{DESCRIPTION}"
  homepage "{HOMEPAGE}"

  auto_updates true
  depends_on arch: :arm64
  depends_on macos: :ventura

  app "LightTable.app"
  binary "#{{appdir}}/LightTable.app/Contents/MacOS/lighttable-cli", target: "lighttable"
end
'''}


def scoop(version: str, url: str, sha256: str, license_name: str,
          license_url: str | None) -> dict[str, str]:
    manifest = {
        "version": version,
        "description": DESCRIPTION,
        "homepage": HOMEPAGE,
        "license": ({"identifier": license_name, "url": license_url}
                    if license_url else license_name),
        "architecture": {"64bit": {"url": url, "hash": sha256}},
        "extract_dir": "LightTable",
        "bin": [["lighttable.cmd", "lighttable"]],
        "shortcuts": [["LightTable.exe", "LightTable"]],
    }
    return {"scoop/bucket/lighttable.json": json.dumps(manifest, indent=4) + "\n"}


def yaml_document(kind: str, content: str) -> str:
    return (f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.{kind}."
            f"{MANIFEST_VERSION}.schema.json\n\n{content}"
            f'ManifestType: {kind}\nManifestVersion: {MANIFEST_VERSION}\n')


def winget(version: str, url: str, sha256: str, license_name: str,
           license_url: str | None) -> dict[str, str]:
    # JSON strings are also valid quoted YAML scalars; avoid license text injection.
    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    prefix = f"winget/manifests/n/NicholasReville/LightTable/{version}/{PACKAGE_ID}"
    identity = f'PackageIdentifier: {PACKAGE_ID}\nPackageVersion: "{version}"\n'
    locale = (identity + f"PackageLocale: en-US\nPublisher: {PUBLISHER}\n"
              f"PublisherUrl: https://github.com/reville\nPackageName: LightTable\n"
              f"PackageUrl: {HOMEPAGE}\nLicense: {quote(license_name)}\n")
    if license_url:
        locale += f"LicenseUrl: {quote(license_url)}\n"
    locale += (f"ShortDescription: {DESCRIPTION}\nMoniker: lighttable\n"
               f"ReleaseNotesUrl: {HOMEPAGE}/releases/tag/v{version}\n")
    installer = identity + f'''InstallerType: nullsoft
Scope: user
InstallModes:
  - interactive
  - silent
InstallerSwitches:
  Silent: /S
  SilentWithProgress: /S
  InstallLocation: /D=<INSTALLPATH>
UpgradeBehavior: install
Commands:
  - lighttable
Installers:
  - Architecture: x64
    InstallerUrl: {url}
    InstallerSha256: "{sha256.upper()}"
    AppsAndFeaturesEntries:
      - DisplayName: LightTable
        Publisher: {PUBLISHER}
        DisplayVersion: "{version}"
        ProductCode: LightTable
'''
    return {
        prefix + ".yaml": yaml_document("version", identity + "DefaultLocale: en-US\n"),
        prefix + ".locale.en-US.yaml": yaml_document("defaultLocale", locale),
        prefix + ".installer.yaml": yaml_document("installer", installer),
    }


def chocolatey(version: str, url: str, sha256: str, license_url: str,
               require_license_acceptance: bool) -> dict[str, str]:
    package = ET.Element("package", xmlns="http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd")
    metadata = ET.SubElement(package, "metadata")
    for key, value in {
        "id": "lighttable", "version": version, "title": "LightTable",
        "authors": PUBLISHER, "projectUrl": HOMEPAGE,
        "licenseUrl": license_url,
        "requireLicenseAcceptance": str(require_license_acceptance).lower(),
        "summary": DESCRIPTION, "description": DESCRIPTION + ". Includes the lighttable command-line interface.",
        "releaseNotes": f"{HOMEPAGE}/releases/tag/v{version}",
        "tags": "photography raw film darkroom",
        "projectSourceUrl": HOMEPAGE,
        "packageSourceUrl": f"{HOMEPAGE}/blob/main/scripts/generate-installers.py",
        "bugTrackerUrl": f"{HOMEPAGE}/issues",
    }.items():
        ET.SubElement(metadata, key).text = value
    files = ET.SubElement(package, "files")
    ET.SubElement(files, "file", src="tools\\**", target="tools")
    ET.indent(package, space="  ")
    install = f'''$ErrorActionPreference = 'Stop'
$packageArgs = @{{
  packageName = 'lighttable'
  fileType = 'exe'
  url64bit = '{url}'
  checksum64 = '{sha256}'
  checksumType64 = 'sha256'
  silentArgs = '/S'
  validExitCodes = @(0)
}}
Install-ChocolateyPackage @packageArgs
'''
    uninstall = r'''$ErrorActionPreference = 'Stop'
# Read the same registry view as the x64 NSIS installer, even from 32-bit PowerShell.
$registry = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
  [Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
$entry = $null
try {
  $entry = $registry.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall\LightTable')
  if ($null -eq $entry) { return }
  $installLocation = $entry.GetValue('InstallLocation')
} finally {
  if ($null -ne $entry) { $entry.Dispose() }
  $registry.Dispose()
}
# The NSIS installer owns application files and PATH. User catalogs are separate.
if ($installLocation) {
  $uninstaller = Join-Path $installLocation 'Uninstall.exe'
  if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'LightTable uninstaller is missing.' }
  Uninstall-ChocolateyPackage -PackageName 'lighttable' -FileType 'exe' `
    -SilentArgs '/S' -File $uninstaller -ValidExitCodes @(0)
} else {
  throw 'LightTable InstallLocation is missing.'
}
'''
    return {
        "chocolatey/lighttable/lighttable.nuspec": ET.tostring(package, encoding="unicode") + "\n",
        "chocolatey/lighttable/tools/chocolateyinstall.ps1": install,
        "chocolatey/lighttable/tools/chocolateyuninstall.ps1": uninstall,
    }


def generate(version: str, artifacts_dir: Path, channels: list[str] | None = None,
             license_name: str | None = None, license_url: str | None = None,
             require_license_acceptance: bool = False,
             source_revision: str | None = None) -> dict[str, str]:
    version = release_version(version)
    validate_license(license_name, license_url)
    if not artifacts_dir.is_dir():
        raise ValueError(f"artifacts directory does not exist: {artifacts_dir}")
    names = {channel: template.format(version=version)
             for channel, template in CHANNEL_ARTIFACTS.items()}
    if channels is None:
        channels = [channel for channel, name in names.items() if (artifacts_dir / name).exists()]
    if not channels:
        raise ValueError("no supported release artifacts found")
    for channel in channels:
        if channel not in names:
            raise ValueError(f"unsupported installer channel: {channel}")
        if not (artifacts_dir / names[channel]).is_file():
            raise ValueError(f"{channel} requires {names[channel]}")
    if set(channels) & {"scoop", "winget", "chocolatey"} and not license_name:
        raise ValueError("Windows installer manifests require --license; choose the app license explicitly")
    if "chocolatey" in channels and not license_url:
        raise ValueError("Chocolatey requires --license-url")
    if "scoop" in channels:
        validate_portable_archive(artifacts_dir / names["scoop"])
    if "aur" in channels and not source_revision:
        raise ValueError("AUR manifests require --source-revision for the exact release commit")
    hashes = {name: digest(artifacts_dir / name) for name in sorted(set(names.values()))
              if (artifacts_dir / name).exists()}
    # Include optional Sparkle release files when CI has placed them beside installers.
    for name in (f"LightTable-{version}-macos-arm64.zip", "appcast.xml"):
        if (artifacts_dir / name).exists():
            hashes[name] = digest(artifacts_dir / name)
    output = {}
    for channel in dict.fromkeys(channels):
        name = names[channel]
        args = (version, f"{HOMEPAGE}/releases/download/v{version}/{name}", hashes[name])
        if channel == "homebrew":
            output.update(homebrew(*args))
        elif channel == "scoop":
            output.update(scoop(*args, license_name, license_url))
        elif channel == "winget":
            output.update(winget(*args, license_name, license_url))
        elif channel == "chocolatey":
            output.update(chocolatey(*args, license_url, require_license_acceptance))
        elif channel == "aur":
            spec = importlib.util.spec_from_file_location(
                "lighttable_aur_package", Path(__file__).parent / "linux/make-aur-package.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory(prefix="lighttable-aur-manifest-") as temporary:
                directory = Path(temporary) / "recipe"
                module.generate(artifacts_dir / name, directory, version=version,
                                source_revision=source_revision)
                for item in sorted(directory.iterdir()):
                    output["aur/lighttable-bin/" + item.name] = item.read_text(encoding="utf-8")
    output["SHA256SUMS"] = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="stable X.Y.Z version, without v")
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="new or empty output directory")
    parser.add_argument("--channels", nargs="+", choices=tuple(CHANNEL_ARTIFACTS),
                        help="default: all channels whose release artifact is present")
    parser.add_argument("--license", dest="license_name", help="chosen license name or SPDX identifier")
    parser.add_argument("--license-url", help="public HTTPS page with the chosen license")
    parser.add_argument("--source-revision", help="exact full source commit required for Linux release manifests")
    parser.add_argument("--require-license-acceptance", action="store_true",
                        help="set the Chocolatey license acceptance metadata")
    args = parser.parse_args()
    try:
        output = generate(args.version, args.artifacts_dir, args.channels, args.license_name,
                          args.license_url, args.require_license_acceptance, args.source_revision)
        if args.output_dir.exists() and (not args.output_dir.is_dir() or any(args.output_dir.iterdir())):
            raise ValueError("output directory must be new or empty; use a fresh directory for each release")
        if args.output_dir.is_symlink():
            raise ValueError("output directory cannot be a symlink")
        args.output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lighttable-installers-", dir=args.output_dir.parent) as temp:
            staged = Path(temp) / "generated"
            staged.mkdir()
            for relative, content in output.items():
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            if args.output_dir.exists():
                args.output_dir.rmdir()
            staged.rename(args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Generated {len(output)} files in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
