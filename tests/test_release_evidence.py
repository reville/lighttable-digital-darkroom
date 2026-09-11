# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name, ROOT/'scripts/release'/f'{name}.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
mac=load('macos_receipt')

class MacReceiptTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.root=Path(t.name);self.app=self.root/'LightTable.app';(self.app/'Contents').mkdir(parents=True)
        self.info={'LightTableSourceDirty':False,'LightTableSourceRevision':'a'*40,'CFBundleShortVersionString':'0.7.0-beta.1','LSMinimumSystemVersion':'14.0','SUEnableAutomaticChecks':False}
        self.native={'ok':True,'layer':'package','fixtureCount':2,'machine':{'machine':'arm64'}}
        self.dmg=self.root/'LightTable-0.7.0-beta.1-macos-arm64.dmg';self.dmg.write_bytes(b'final signed image')
    def write(self): (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps(self.info))
    @patch.object(mac,'checked')
    def test_manual_beta_records_actual_bytes_without_notarization_claim(self, check):
        self.write();r=mac.create(self.app,self.native,[self.dmg],'beta')
        self.assertTrue(r['code_signature_verified']);self.assertFalse(r['notarization_verified'])
        self.assertEqual(r['artifacts'][0]['bytes'],len(b'final signed image'))
        self.assertEqual(check.call_count,1)
    @patch.object(mac,'checked')
    def test_dirty_source_failed_native_and_enabled_beta_updates_are_rejected(self, check):
        for field,value in [('LightTableSourceDirty',True),('SUEnableAutomaticChecks',True)]:
            original=self.info[field];self.info[field]=value;self.write()
            with self.assertRaises(AssertionError):mac.create(self.app,self.native,[self.dmg],'beta')
            self.info[field]=original
        self.write()
        with self.assertRaises(AssertionError):mac.create(self.app,{**self.native,'ok':False},[self.dmg],'beta')
    @patch.object(mac,'checked')
    def test_stable_requires_application_and_dmg_trust_checks(self, check):
        self.info['CFBundleShortVersionString']='0.7.0'
        self.write();r=mac.create(self.app,self.native,[self.dmg],'stable')
        self.assertTrue(r['notarization_verified'])
        self.assertTrue(any(c.args[:2]==('spctl','--assess') for c in check.call_args_list))
        self.assertEqual(sum(c.args[:3]==('xcrun','stapler','validate') for c in check.call_args_list),2)

if __name__=='__main__':unittest.main()
