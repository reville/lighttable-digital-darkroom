# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import json
import tempfile
import hashlib
import tarfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("distribution", ROOT / "scripts/release/publish-distribution.py")
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


def promotion(platform="windows-x64", version="0.7.6", source="a" * 40):
    suffix = "windows-x64-setup.exe" if platform == "windows-x64" else "macos-arm64.dmg"
    name = f"LightTable-{version}-{suffix}" if platform == "windows-x64" else f"LightTable-{version}-macos-arm64.dmg"
    tag = ("macos-v" if platform == "macos-arm64" and "-beta." in version else "v") + version
    artifact = {"name": name, "bytes": 3, "sha256": "b" * 64, "url": f"https://github.com/reville/lighttable-digital-darkroom/releases/download/{tag}/{name}"}
    manifest = {"schema_version": 1, "version": version, "source_revision": source, "tag": tag, "platforms": {platform: {"version": version, "artifacts": [artifact], "build_run_id": "1", "channel": "beta" if "-beta." in version else "stable", "state": "published", "minimum_os": "macOS 14" if platform == "macos-arm64" else "Windows 10", "update_owner": "manual" if platform == "macos-arm64" else "app", "signing": "ad-hoc" if platform == "macos-arm64" else "authenticode", "gates": [], "validation": {"status": "passed", "receipts": ["https://example.test/receipt"]}}}}
    manifest["platforms"][platform]["build_run_id"] = "1"
    return {"version": version, "source_revision": source, "tag": manifest["tag"], "platform": platform, "build_run_id": 1, "manifest": manifest, "applied": True, "public_bytes_verified": True, "published_release": True, "receipts_verified": True, "published_at": "2026-01-01T00:00:00Z"}


class DistributionSafetyTests(unittest.TestCase):
    def test_promotion_fixture_and_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "promotion.json"; path.write_text(json.dumps(promotion()))
            loaded = distribution.load_promotions([path], "0.7.6", require_public=True)
            self.assertEqual(loaded["windows-x64"]["source_revision"], "a" * 40)
            bad = promotion(version="0.7.5"); path.write_text(json.dumps(bad))
            with self.assertRaises(Exception): distribution.load_promotions([path], "0.7.6", require_public=True)

    def test_homebrew_beta_routing_uses_explicit_mac_promotion(self):
        mac = promotion(platform="macos-arm64", version="0.7.6-beta.1")
        with mock.patch.object(distribution, "required_asset", return_value=mac["manifest"]["platforms"]["macos-arm64"]["artifacts"][0]), \
             mock.patch.object(distribution.subprocess, "run") as run, \
             mock.patch.object(distribution.Path, "read_text", return_value='  version "0.7.6-beta.1"\n  sha256 "old"\n  url "old"\n'):
            with tempfile.TemporaryDirectory() as temp:
                result = distribution.sync_homebrew("0.7.6", mac, Path(temp), push=False)
        self.assertEqual(result["state"], "staged")
        self.assertTrue(any("repo" in str(call.args) for call in run.call_args_list))

    def test_upload_reuses_identical_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "asset.tgz"; path.write_bytes(b"payload")
            digest = distribution.sha256_file(path)
            assets = {"asset.tgz": {"size": path.stat().st_size, "digest": "sha256:" + digest}}
            with mock.patch.object(distribution, "gh") as gh:
                self.assertEqual(distribution.upload_if_needed("v0.7.6", path, assets), "reused"); gh.assert_not_called()
            assets["asset.tgz"]["size"] += 1
            with self.assertRaises(ValueError): distribution.upload_if_needed("v0.7.6", path, assets)

    def test_main_reports_staged_without_false_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"; evidence = Path(temp) / "promotion.json"; evidence.write_text("{}")
            with mock.patch.object(distribution, "load_promotions", return_value={"windows-x64": promotion()}), \
                 mock.patch.object(distribution, "sync_npm", return_value={"channel": "npm", "state": "staged"}), \
                 mock.patch("sys.argv", ["publisher", "--version", "0.7.6", "--npm", "--promotion-result", str(evidence), "--output", str(output)]):
                self.assertEqual(distribution.main(), 0)
            self.assertEqual(json.loads(output.read_text())["outcomes"][0]["state"], "staged")

    def test_dispatch_pending_resume_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"; output.write_text(json.dumps({"schema": 1, "version": "0.7.6", "source_revision": "a" * 40, "outcomes": [{"channel": "npm", "state": "unverified", "dispatch_pending": True}]}))
            evidence = Path(temp) / "promotion.json"; evidence.write_text("{}")
            with mock.patch.object(distribution, "load_promotions", return_value={"windows-x64": promotion()}), mock.patch("sys.argv", ["publisher", "--version", "0.7.6", "--npm", "--promotion-result", str(evidence), "--output", str(output)]):
                with self.assertRaises(SystemExit): distribution.main()

    def test_missing_digest_is_rejected(self):
        asset = {"name": "x", "bytes": 1, "sha256": "b" * 64}
        self.assertIsNone(distribution.asset_digest({"digest": "sha256:not-a-digest"}))
        self.assertEqual(distribution.asset_digest({"digest": "sha256:" + "c" * 64}), "c" * 64)

    def test_real_stage_npm_keeps_license_and_release_metadata(self):
        version = "0.7.6"
        name = f"LightTable-{version}-windows-x64-setup.exe"
        payload = b"tiny installer fixture"
        asset = {"name": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        promotion_record = promotion(platform="windows-x64", version=version)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            setup = directory / name
            setup.write_bytes(payload)
            with mock.patch.object(distribution, "required_asset", return_value=asset):
                tarball = distribution.stage_npm(version, promotion_record, directory)
            self.assertTrue(tarball.is_file())
            with tarfile.open(tarball, "r:gz") as archive:
                names = archive.getnames()
                self.assertTrue(any(name.endswith("/LICENSE") for name in names))
                package_json = json.loads(archive.extractfile(next(n for n in names if n.endswith("/package.json"))).read())
                release_json = json.loads(archive.extractfile(next(n for n in names if n.endswith("/release.json"))).read())
            self.assertEqual(package_json["version"], version)
            self.assertEqual(release_json["version"], version)
            self.assertIn(asset["sha256"], [item["sha256"] for item in release_json["platforms"].values()])

    def test_missing_public_proof_is_rejected(self):
        record = promotion()
        record["published_release"] = False
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "promotion.json"
            path.write_text(json.dumps(record))
            with self.assertRaises(Exception):
                distribution.load_promotions([path], "0.7.6", require_public=True)



if __name__ == "__main__": unittest.main()
