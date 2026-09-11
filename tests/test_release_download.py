# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('download_upgrade', ROOT/'scripts/release/download_upgrade.py')
m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class PublicDownloadTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.directory=Path(tmp.name)/'candidate'
        self.name='LightTable-0.6.0-linux-x86_64.tar.gz';self.data=b'published archive bytes'
        self.asset=dict(name=self.name,browser_download_url=f'https://github.com/{m.REPO}/releases/download/v0.6.0/{self.name}',size=len(self.data),digest='sha256:'+hashlib.sha256(self.data).hexdigest())
        self.release=dict(draft=False,prerelease=False,assets=[self.asset])
    def run_download(self, data=None):
        def curl(*args,**kwargs):(self.directory/self.name).write_bytes(self.data if data is None else data)
        with patch.object(m.subprocess,'check_output',return_value=json.dumps(self.release).encode()), patch.object(m.subprocess,'run',side_effect=curl):
            return m.download('0.6.0',self.directory)
    def test_verified_public_download(self):self.assertEqual(self.run_download()['sha256'],self.asset['digest'][7:])
    def test_changed_download_and_incomplete_digest_rejected(self):
        with self.assertRaises(ValueError):self.run_download(b'altered')
        self.asset['digest']=''
        with self.assertRaises(ValueError):self.run_download()
    def test_unpublished_or_external_asset_rejected(self):
        self.release['draft']=True
        with self.assertRaises(ValueError):self.run_download()
        self.release['draft']=False;self.asset['browser_download_url']='https://example.com/file'
        with self.assertRaises(ValueError):self.run_download()

if __name__=='__main__':unittest.main()
