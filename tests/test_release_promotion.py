"""Promotion consumes immutable binaries and actual client receipts; no external writes."""
from __future__ import annotations

import argparse
import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import promote_candidate as promote
import release_process as release


class PromotionProofTests(unittest.TestCase):
    def manifest(self, platform='windows-x64'):
        return {'schema_version': 1, 'repository': release.REPOSITORY, 'tag': 'v0.7.0',
                'version': '0.7.0', 'source_revision': 'a' * 40, 'platforms': {platform: {
                    'version': '0.7.0', 'channel': 'stable', 'minimum_os': 'Windows 10 x64',
                    'update_owner': 'manual', 'signing': 'authenticode', 'state': 'candidate',
                    'validation': {'status': 'pending', 'receipts': []}, 'gates': [],
                    'artifacts': [], 'build_run_id': '123'}}}

    def native(self):
        return {'ok': True, 'build': {'source_revision': 'a' * 40, 'version': '0.7.0',
                'source_dirty': False, 'platform': 'windows', 'architecture': 'x64', 'authenticode_signed': True},
                'export': {'bits': 16}, 'edit_persistence': {'server_restarted': True}}

    def test_windows_requires_actual_offline_native_client_receipts(self):
        fixture = {'host.json': {'ok': True, 'product_type': 1, 'architecture': 'AMD64', 'build': '26200',
                   'webview2_absent_before_offline_install': True, 'network_adapters_disabled': True,
                   'installer_signature_before_disconnect': 'Valid'},
                   'result.json': {'ok': True, 'offline': True, 'account_is_elevated': False,
                                   'installer_sha256': 'b' * 64},
                   'installed-signatures.json': {'pe_count': 1, 'invalid_count': 0,
                                                'signatures': [{'path': 'Uninstall.exe', 'status': 'Valid'}]},
                   'native/report.json': self.native()}
        mutations = [None,
            ('host.json', 'product_type', 3), ('host.json', 'architecture', 'ARM64'),
            ('host.json', 'webview2_absent_before_offline_install', False),
            ('host.json', 'network_adapters_disabled', False), ('host.json', 'build', '19045'),
            ('result.json', 'account_is_elevated', True), ('result.json', 'offline', False),
            ('result.json', 'installer_sha256', 'c' * 64), ('native/report.json', 'ok', False),
            ('installed-signatures.json', 'invalid_count', 1)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                data = copy.deepcopy(fixture)
                if mutation:
                    filename, key, value = mutation
                    data[filename][key] = value
                for filename, record in data.items():
                    path = directory / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record))
                if mutation:
                    with self.assertRaises(ValueError):
                        promote.verify_windows_client(directory, '11', self.manifest(), 'b' * 64)
                else:
                    promote.verify_windows_client(directory, '11', self.manifest(), 'b' * 64)

    def test_native_source_and_restart_are_required(self):
        for key, value in [('source_revision', 'b' * 40), ('source_dirty', True), ('architecture', 'arm64')]:
            report = self.native()
            report['build'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                promote.verify_native_report(report, self.manifest(), 'windows-x64')
        report = self.native()
        report['edit_persistence']['server_restarted'] = False
        with self.assertRaises(ValueError):
            promote.verify_native_report(report, self.manifest(), 'windows-x64')

    def test_feed_refuses_downgrade_and_same_version_replacement(self):
        def feed(version, signature='fixture'):
            return json.dumps({'signed': {'version': version}, 'signature': signature}).encode()
        promote.check_feed_forward(feed('0.6.0'), feed('0.7.0'), 'linux-x86_64')
        promote.check_feed_forward(feed('0.7.0'), feed('0.7.0'), 'linux-x86_64')
        for old, new in [(feed('0.7.0'), feed('0.6.0')), (feed('0.7.0'), feed('0.7.0', 'different'))]:
            with self.assertRaises(ValueError):
                promote.check_feed_forward(old, new, 'linux-x86_64')

    def test_windows_feed_signature_must_match_installer_and_embedded_key(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.exceptions import InvalidSignature
        except ImportError:
            self.skipTest('cryptography is required')
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        with tempfile.TemporaryDirectory() as temporary, patch.object(promote, 'SPARKLE_KEY', public):
            directory = Path(temporary)
            manifest = self.manifest()
            entry = manifest['platforms']['windows-x64']
            entry['update_owner'] = 'app'
            entry['artifacts'] = [{'name': 'appcast-windows-x64.xml'}]
            archive_name, installer_name = promote.primary_names(manifest, 'windows-x64')
            archive = directory / archive_name
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('LightTable/LightTable.exe', b'PE fixture ' + public.encode())
            installer = directory / installer_name
            installer.write_bytes(b'signed installer fixture')
            signature = base64.b64encode(key.sign(installer.read_bytes())).decode()
            feed = directory / 'appcast-windows-x64.xml'
            feed.write_text('<rss xmlns:sparkle="' + promote.SPARKLE + '"><channel><item><enclosure '
                'url="' + release.asset_url(manifest, installer_name) + '" length="' + str(installer.stat().st_size) + '" '
                'sparkle:version="0.7.0" sparkle:edSignature="' + signature + '"/></item></channel></rss>')
            self.assertEqual(promote.verify_signed_feed(manifest, 'windows-x64', directory), feed)
            installer.write_bytes(b'tampered installer bytes')
            with self.assertRaises((ValueError, InvalidSignature)):
                promote.verify_signed_feed(manifest, 'windows-x64', directory)
            installer.write_bytes(b'signed installer fixture')
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('LightTable/LightTable.exe', b'PE fixture with another update key')
            with self.assertRaisesRegex(ValueError, 'public key'):
                promote.verify_signed_feed(manifest, 'windows-x64', directory)

    def test_binary_identity_must_match_original_build_not_only_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest()
            for name in promote.primary_names(manifest, 'windows-x64'):
                (root / name).write_bytes(b'original')
                manifest['platforms']['windows-x64']['artifacts'].append(release.file_identity(root / name))
            promote.verify_original_binaries(manifest, 'windows-x64', root)
            (root / promote.primary_names(manifest, 'windows-x64')[0]).write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                promote.verify_original_binaries(manifest, 'windows-x64', root)

    def test_dry_run_checks_evidence_without_publication_or_feed_mutation(self):
        args = argparse.Namespace(platform='windows-x64', version='0.7.0', source_revision='a' * 40,
            build_run_id='123', manifest_run_id='456', manifest_artifact='LightTable-windows-x64-promotion',
            native_run_id='789', apply=False, make_public=True, advance_feed=False)
        manifest = self.manifest()
        def download(run_id, name, directory):
            directory.mkdir()
            if name.endswith('-promotion'):
                release.write_json(directory / release.platform_names('0.7.0', args.platform)['manifest'], manifest)
            return directory
        def gh(*command):
            if command[0] == 'api':
                return 'a' * 40
            self.assertEqual(command[:2], ('release', 'view'))
            return json.dumps({'isDraft': True, 'isPrerelease': False})
        with patch.object(promote, 'build_run', return_value={'head_sha': 'd' * 40}), patch.object(promote, 'workflow_run') as runs, patch.object(promote, 'download_artifact', side_effect=download), \
             patch.object(release, 'verify_artifacts'), patch.object(promote, 'verify_original_binaries'), \
             patch.object(promote, 'verify_evidence') as evidence, patch.object(promote, 'verify_signed_feed', return_value=None), \
             patch.object(release, 'gh', side_effect=gh), patch.object(promote, 'release_state', return_value={'isDraft': True, 'isPrerelease': False}), \
             patch.object(release, 'publish', return_value={'assets': [], 'applied': False}) as publish, \
             patch.object(promote, 'public_download') as public, patch.object(promote, 'advance_feed') as feed:
            result = promote.promote(args)
            self.assertFalse(result['applied'])
            self.assertTrue(result['receipts_verified'])
            self.assertEqual(result['source_revision'], args.source_revision)
            evidence.assert_called_once()
            self.assertFalse(publish.call_args.kwargs['apply'])
            public.assert_not_called()
            feed.assert_not_called()

    def test_missing_release_is_planned_without_writes_and_created_only_on_apply(self):
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                manifest = self.manifest()
                entry = manifest['platforms']['windows-x64']
                entry['state'] = 'ready'
                entry['validation'] = {'status': 'passed', 'receipts': ['https://example.test/receipt']}
                name = 'LightTable-0.7.0-windows-x64-setup.exe'
                entry['artifacts'] = [{'name': name, 'bytes': 7, 'sha256': 'b' * 64,
                                       'url': release.asset_url(manifest, name)}]
                manifest_path = directory / release.platform_names('0.7.0', 'windows-x64')['manifest']
                release.write_json(manifest_path, manifest)
                states = [None, {'isDraft': True, 'isPrerelease': False}] if apply else [None]
                with patch.object(promote, 'release_state', side_effect=states), patch.object(release, 'gh') as gh, \
                     patch.object(release, 'publish', return_value={'applied': apply, 'assets': []}) as publish:
                    result, state = promote.plan_or_publish(manifest_path, directory, 'windows-x64', apply)
                    self.assertTrue(result['create_draft'])
                    self.assertEqual(result['draft_created'], apply)
                    self.assertTrue(state['isDraft'])
                    if apply:
                        self.assertEqual(gh.call_args.args[:3], ('release', 'create', 'v0.7.0'))
                        self.assertIn('--verify-tag', gh.call_args.args)
                        self.assertIn('--draft', gh.call_args.args)
                        self.assertNotIn('--clobber', gh.call_args.args)
                        self.assertTrue(publish.call_args.kwargs['apply'])
                    else:
                        gh.assert_not_called()
                        publish.assert_not_called()
                        self.assertEqual(len(result['assets']), 2)
                        self.assertTrue(all(item['action'] == 'add' for item in result['assets']))

    def test_release_listing_errors_do_not_trigger_draft_creation(self):
        with patch.object(release, 'gh', return_value='[[]]'):
            self.assertIsNone(promote.release_state('v0.7.0'))
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / 'manifest.json'
            release.write_json(manifest_path, self.manifest())
            for failure in (subprocess.CalledProcessError(1, ['gh', 'api']), json.JSONDecodeError('bad response', '', 0)):
                with self.subTest(failure=type(failure).__name__), \
                     patch.object(release, 'gh', side_effect=failure) as gh, patch.object(release, 'publish') as publish:
                    with self.assertRaises(type(failure)):
                        promote.plan_or_publish(manifest_path, Path(temporary), 'windows-x64', True)
                    self.assertEqual(gh.call_args.args[0], 'api')
                    self.assertEqual(gh.call_count, 1)
                    publish.assert_not_called()

    def test_failed_evidence_never_calls_publish(self):
        args = argparse.Namespace(platform='windows-x64', version='0.7.0', source_revision='a' * 40,
            build_run_id='123', manifest_run_id='456', manifest_artifact='LightTable-windows-x64-promotion',
            native_run_id='789', apply=True, make_public=True, advance_feed=False)
        manifest = self.manifest()
        def download(run_id, name, directory):
            directory.mkdir()
            if name.endswith('-promotion'):
                release.write_json(directory / release.platform_names('0.7.0', args.platform)['manifest'], manifest)
            return directory
        with patch.object(promote, 'build_run', return_value={'head_sha': 'd' * 40}), patch.object(promote, 'workflow_run'), patch.object(promote, 'download_artifact', side_effect=download), \
             patch.object(release, 'verify_artifacts'), patch.object(promote, 'verify_original_binaries'), \
             patch.object(promote, 'verify_evidence', side_effect=ValueError('client proof missing')), \
             patch.object(release, 'gh', return_value='a' * 40), patch.object(release, 'publish') as publish:
            with self.assertRaisesRegex(ValueError, 'client proof missing'):
                promote.promote(args)
            publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
