import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
from update_index import merge


class ReleaseIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = json.loads((ROOT / 'release/manifest.json').read_text())
        incoming = copy.deepcopy(self.index)
        incoming['platforms'] = {'linux-x86_64': incoming['platforms']['linux-x86_64']}
        incoming['platforms']['linux-x86_64']['artifacts'] = incoming['platforms']['linux-x86_64']['artifacts'][:2]
        self.result = dict(applied=True, public_bytes_verified=True, published_release=True,
                           receipts_verified=True, manifest=incoming, platform='linux-x86_64',
                           version=incoming['version'], source_revision=incoming['source_revision'],
                           published_at='2026-09-10T03:51:06Z')

    def test_resume_preserves_arch_and_other_platforms(self):
        out = merge(self.index, self.result)
        self.assertEqual(len(out['platforms']['linux-x86_64']['artifacts']), 4)
        self.assertEqual(out['platforms']['macos-arm64']['artifacts'], self.index['platforms']['macos-arm64']['artifacts'])
        self.assertEqual(out['platforms']['windows-x64']['state'], 'blocked')
        self.assertNotIn('source_revision', self.index['platforms']['macos-arm64'])

    def test_new_linux_version_preserves_older_mac_source(self):
        m = self.result['manifest'];m['version'] = '0.7.0';m['source_revision'] = 'a'*40
        e = m['platforms']['linux-x86_64'];e['version'] = '0.7.0';e['tag'] = 'v0.7.0'
        for a in e['artifacts']:
            a['name'] = a['name'].replace('0.6.0','0.7.0');a['url'] = a['url'].replace('0.6.0','0.7.0')
        self.result.update(version='0.7.0', source_revision='a'*40)
        out = merge(self.index, self.result)
        self.assertEqual(out['version'], '0.7.0')
        self.assertEqual(out['platforms']['macos-arm64']['source_revision'], self.index['source_revision'])
        self.assertEqual(out['platforms']['macos-arm64']['version'], '0.6.0-beta.1')

    def test_unpublished_unverified_or_changed_assets_are_rejected(self):
        for flag in ('applied', 'public_bytes_verified', 'published_release', 'receipts_verified'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                merge(self.index, {**self.result, flag: False})
        self.result['manifest']['platforms']['linux-x86_64']['artifacts'][0]['sha256'] = 'a'*64
        with self.assertRaises(ValueError):merge(self.index, self.result)

if __name__ == '__main__':unittest.main()
