# SPDX-License-Identifier: GPL-3.0-only
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/"scripts/release"))
import orchestrate
class T(unittest.TestCase):
 def setUp(self): self.d=tempfile.TemporaryDirectory(); self.state=Path(self.d.name)/"state.json"; self.src="a"*40
 def tearDown(self): self.d.cleanup()
 def a(self,*x): return ["--version","0.7.6","--source-revision",self.src,"--state",str(self.state),*x]
 def test_inputs_beta_hash_and_verify_promote_separate(self):
  with patch.object(orchestrate,"gh",return_value=json.dumps({"object":{"type":"commit","sha":self.src}})):
   ns=orchestrate.main(self.a("--stage","build","--macos-channel","beta")); self.assertEqual(ns,0)
  # pure input validation
  args=__import__("argparse").Namespace(stage="vm",build_run_id="12",installer_sha256="b"*64,source_revision=self.src,version="0.7.6")
  self.assertEqual(orchestrate.inputs(args,{})["installer_sha256"],"b"*64)
 def test_status_reads_recorded_only(self):
  self.state.write_text(json.dumps({"schema_version":1,"repository":orchestrate.REPOSITORY,"version":"0.7.6","source_revision":self.src,"tag":"v0.7.6","runs":{}}))
  with patch.object(orchestrate,"api") as api: self.assertEqual(orchestrate.main(self.a("--stage","status")),0); api.assert_not_called()
 def test_no_url_pending_and_lock(self):
  def fake(*xs):
   if xs[0]=="api": return json.dumps({"object":{"type":"commit","sha":self.src}})
   return ""
  with patch.object(orchestrate,"gh",side_effect=fake): self.assertEqual(orchestrate.main(self.a("--stage","build","--apply")),2)
  data=json.loads(self.state.read_text()); self.assertIn("build",data["runs"]); self.assertNotIn("run_id",data["runs"]["build"])
 def test_wrong_recovery_run_rejected(self):
  with patch.object(orchestrate,"api",side_effect=[{"object":{"type":"commit","sha":self.src}},{"id":9,"head_repository":{"full_name":"other/x"},"event":"workflow_dispatch","path":".github/workflows/release.yml","head_sha":self.src,"display_title":"x"}]):
   self.assertEqual(orchestrate.main(self.a("--stage","build","--apply","--record-run-id","9")),2)
 def test_live_flags_require_make_public(self):
  args=__import__("argparse").Namespace(stage="promote",build_run_id="1",preparation_run_id="2",native_run_id="3",platform="linux-x86_64",version="0.7.6",source_revision=self.src,macos_channel="stable",macos_version=None,installer_sha256=None,make_public=False,advance_feed=False)
  with self.assertRaises(orchestrate.OrchestrationError): orchestrate.inputs(args,{})
if __name__=="__main__": unittest.main()
