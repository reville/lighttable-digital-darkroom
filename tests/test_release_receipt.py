# SPDX-License-Identifier: GPL-3.0-only
import copy, importlib.util, json, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('receipt',ROOT/'scripts/release/generate-receipt.py'); receipt=importlib.util.module_from_spec(spec); spec.loader.exec_module(receipt)
class ReceiptTests(unittest.TestCase):
 def setUp(self):
  self.aggregate=json.loads((ROOT/'release/manifest.json').read_text()); self.version=self.aggregate['version']; self.source=self.aggregate['source_revision']
 def promotion(self, platform='linux-x86_64', **flags):
  entry=copy.deepcopy(self.aggregate['platforms'][platform]); m={'schema_version':1,'version':entry['version'],'source_revision':self.source,'tag':entry.get('tag',self.aggregate.get('tag','v'+entry['version'])),'platforms':{platform:entry}}
  out={'platform':platform,'version':m['version'],'source_revision':self.source,'tag':m['tag'],'build_run_id':entry['build_run_id'],'manifest':m,'applied':True,'public_bytes_verified':True,'published_release':True,'receipts_verified':True,'published_at':'2026-09-13T00:00:00Z'}; out.update(flags); return out
 def test_absent_promotions_do_not_claim_verified(self):
  text=receipt.format_receipt(self.version,self.aggregate,checked_at='2026-09-13T00:00:00-04:00'); self.assertIn('Public promotion proof: 0 of',text); self.assertNotIn('advanced by the verified promotion',text); self.assertIn('- linux-x86_64: advancement not verified',text)
 def test_blocked_flags_and_staged_failed_npm(self):
  p=self.promotion(published_release=False); dist={'schema':1,'version':self.version,'source_revision':self.source,'outcomes':[{'channel':'npm','state':'staged'},{'channel':'scoop','state':'failed'}]}; text=receipt.format_receipt(self.version,self.aggregate,distribution=dist,promotions={'linux-x86_64':p},checked_at='2026-09-13T00:00:00Z'); self.assertIn('| blocked |',text); self.assertIn('npm: staged',text); self.assertIn('scoop: failed',text)
 def test_linux_and_macos_beta_same_source(self):
  p1=self.promotion('linux-x86_64'); p2=self.promotion('macos-arm64'); text=receipt.format_receipt(self.version,self.aggregate,promotions={'linux-x86_64':p1,'macos-arm64':p2},checked_at='2026-09-13T00:00:00Z'); self.assertIn('Public promotion proof: 2 of',text); self.assertIn('macos-v',text)
 def test_wrong_identity_hash_and_missing_sha_rejected(self):
  p=self.promotion(); bad=copy.deepcopy(p); bad['source_revision']='b'*40
  with self.assertRaises(ValueError): receipt.format_receipt(self.version,self.aggregate,promotions={'linux-x86_64':bad})
  bad=copy.deepcopy(self.aggregate); bad['platforms']['linux-x86_64']['artifacts'][0]['sha256']='0'*64
  with self.assertRaises(ValueError): receipt.format_receipt(self.version,bad,promotions={'linux-x86_64':p})
 def test_distribution_identity_and_published_evidence(self):
  bad={'schema':1,'version':self.version,'source_revision':'b'*40,'outcomes':[]}
  with self.assertRaises(ValueError): receipt.format_receipt(self.version,self.aggregate,distribution=bad)
  bad={'schema':1,'version':self.version,'source_revision':self.source,'outcomes':[{'channel':'npm','state':'published','verified':True}]}
  with self.assertRaises(ValueError): receipt.format_receipt(self.version,self.aggregate,distribution=bad)
if __name__=='__main__': unittest.main()
