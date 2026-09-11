# SPDX-License-Identifier: GPL-3.0-only
"""Legacy mask inversion belongs to the whole mask, not its new first component."""
from pathlib import Path
import shutil
import subprocess
import unittest


class BrowserMaskMigrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_legacy_and_canonical_inversion_survive_repeated_normalization(self):
        script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {normalizeMasks} = await import(pathToFileURL(process.argv[1]));
for (const type of ['radial', 'linear', 'brush']) {
  let mask = normalizeMasks([{id:'legacy', type, invert:true}])[0];
  for (let pass=0; pass<3; pass++) {
    assert.equal(mask.invert, true);
    assert.equal(mask.components[0].invert, false);
    mask = normalizeMasks([mask])[0];
  }
  const canonical = normalizeMasks([{type, invert:true, components:[{type, invert:true}]}])[0];
  assert.equal(canonical.invert, true);
  assert.equal(canonical.components[0].invert, true);
}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script,
                                 str(Path(__file__).resolve().parents[1]/'web/editor-panels.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
