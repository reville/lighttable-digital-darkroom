# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('cache_check', Path(__file__).resolve().parents[1] / 'scripts/ci/check-converted-model.py')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ConvertedModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'denoise.mlpackage'
        self.package.mkdir()
        (self.package / 'weights.bin').write_bytes(b'fixture model')
        for name in ('SCUNet-CODE-LICENSE.txt', 'SCUNet-WEIGHTS-LICENSE.txt'):
            (self.root / name).write_text('fixture license')
        self.entry = dict(version=f'SCUNet {CHECK.CONVERTER.SCUNET_REVISION[:7]} fp16',
                          source='https://github.com/cszn/SCUNet',
                          weightsSha256=CHECK.FETCHER.WEIGHTS['scunet_color_real_psnr.pth'],
                          input=CHECK.CONVERTER.TILE, precision='fp16',
                          sha256=CHECK.CONVERTER.package_digest(self.package))
        self.write_index()

    def write_index(self):
        (self.root / 'models.json').write_text(json.dumps({'denoise': self.entry}))

    def test_valid_cache_and_tampered_bytes(self):
        self.assertEqual(CHECK.verify(self.root), self.entry['sha256'])
        (self.package / 'weights.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'sha256'):
            CHECK.verify(self.root)

    def test_wrong_conversion_identity(self):
        for key, value in [('weightsSha256', '0' * 64), ('precision', 'fp32'), ('input', 256)]:
            with self.subTest(key=key):
                old = self.entry[key]
                self.entry[key] = value
                self.write_index()
                with self.assertRaisesRegex(ValueError, key):
                    CHECK.verify(self.root)
                self.entry[key] = old

    def test_missing_license_and_symlink_refused(self):
        license_file = self.root / 'SCUNet-CODE-LICENSE.txt'
        license_file.unlink()
        with self.assertRaisesRegex(ValueError, 'license'):
            CHECK.verify(self.root)
        license_file.symlink_to(self.root / 'SCUNet-WEIGHTS-LICENSE.txt')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            CHECK.verify(self.root)


if __name__ == '__main__':
    unittest.main()
