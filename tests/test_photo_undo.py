# SPDX-License-Identifier: GPL-3.0-only
"""Behavioral checks for session undo across photo navigation and bounded memory."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node required')
class PhotoUndoTests(unittest.TestCase):
    def run_js(self, body):
        script = "import {createPhotoUndoHistory} from './web/photo-undo.js';\n" + body
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                cwd=ROOT, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_revisit_restores_independent_undo_and_redo(self):
        result = self.run_js("""
const h = createPhotoUndoHistory();
const a = h.activate('source1:a.jpg');
h.push('a0'); h.push('a1');
const undoA = h.undo('a2');
const b = h.activate('source2:a.jpg');
h.push('b0');
const undoB = h.undo('b1');
const revisited = h.activate('source1:a.jpg');
const before = {undo: [...a.undo], redo: [...a.redo]};
const redoA = h.redo(undoA);
console.log(JSON.stringify({undoA, undoB, redoA, before,
  sameEntry: a === revisited, sameArrays: a.undo === revisited.undo,
  a, b, empty: h.redo('a2')}));
""")
        self.assertEqual((result['undoA'], result['undoB'], result['redoA']), ('a1', 'b0', 'a2'))
        self.assertEqual(result['before'], {'undo': ['a0'], 'redo': ['a2']})
        self.assertTrue(result['sameEntry'])
        self.assertTrue(result['sameArrays'])
        self.assertEqual(result['a'], {'undo': ['a0', 'a1'], 'redo': []})
        self.assertEqual(result['b'], {'undo': [], 'redo': ['b1']})
        self.assertIsNone(result['empty'])

    def test_new_edit_and_external_update_clear_redo(self):
        result = self.run_js("""
const h = createPhotoUndoHistory();
const a = h.activate('a');
h.push('a0'); h.push('a1'); h.undo('a2');
const duplicate = h.push('a0');
const redoAfterEdit = h.redo('a3');
h.push('a3');
const externalUndo = h.undo('external');
const externalRedo = h.redo(externalUndo);
const b = h.activate('b'); h.push('b0');
h.clear('a');
const clearedA = h.activate('a');
const sameActive = clearedA;
h.push('new'); h.clear();
console.log(JSON.stringify({duplicate, redoAfterEdit, externalUndo, externalRedo,
  clearedA, activeArrayPreserved: sameActive === h.activate('a'), b}));
""")
        self.assertTrue(result['duplicate'])
        self.assertIsNone(result['redoAfterEdit'])
        self.assertEqual((result['externalUndo'], result['externalRedo']), ('a3', 'external'))
        self.assertEqual(result['clearedA'], {'undo': [], 'redo': []})
        self.assertTrue(result['activeArrayPreserved'])
        self.assertEqual(result['b']['undo'], ['b0'])

    def test_lru_eviction_preserves_active_photo_and_revisit_order(self):
        result = self.run_js("""
const h = createPhotoUndoHistory({maxPhotos: 2});
const a = h.activate('a'); h.push('a0');
const b = h.activate('b'); h.push('b0');
h.activate('a');
const c = h.activate('c'); h.push('c0');
const after = {a: [...a.undo], b: [...b.undo], c: [...c.undo], size: h.size};
const activeSame = c === h.activate('c');
const evicted = h.activate('b');
console.log(JSON.stringify({after, activeSame, evicted}));
""")
        self.assertEqual(result['after'], {'a': ['a0'], 'b': [], 'c': ['c0'], 'size': 2})
        self.assertTrue(result['activeSame'])
        self.assertEqual(result['evicted'], {'undo': [], 'redo': []})

    def test_step_limit_keeps_nearest_states_through_undo_and_redo(self):
        result = self.run_js("""
const h = createPhotoUndoHistory({maxSteps: 3});
const a = h.activate('a');
for (const state of ['0', '1', '2', '3', '4']) h.push(state);
const retained = [...a.undo];
const undos = [h.undo('5'), h.undo('4'), h.undo('3'), h.undo('2')];
const redos = [h.redo('2'), h.redo('3'), h.redo('4'), h.redo('5')];
console.log(JSON.stringify({retained, undos, redos, a}));
""")
        self.assertEqual(result['retained'], ['2', '3', '4'])
        self.assertEqual(result['undos'], ['4', '3', '2', None])
        self.assertEqual(result['redos'], ['3', '4', '5', None])
        self.assertEqual(result['a'], {'undo': ['2', '3', '4'], 'redo': []})

    def test_byte_limit_evicts_inactive_then_prunes_active_in_place(self):
        result = self.run_js("""
const h = createPhotoUndoHistory({maxBytes: 12});
const a = h.activate('a'); h.push('aa');
const b = h.activate('b'); h.push('bb'); h.push('cc'); h.push('dd');
const before = {a: [...a.undo], b: [...b.undo], bytes: h.bytes, size: h.size};
h.push('ee');
const pruned = [...b.undo];
const undo = h.undo('LONG-SNAPSHOT');
const after = {b, bytes: h.bytes, active: h.activeName};
console.log(JSON.stringify({before, pruned, undo, after,
  same: b === h.activate('b')}));
""")
        self.assertEqual(result['before'], {'a': [], 'b': ['bb', 'cc', 'dd'], 'bytes': 12, 'size': 1})
        self.assertEqual(result['pruned'], ['cc', 'dd', 'ee'])
        self.assertEqual(result['undo'], 'ee')
        self.assertEqual(result['after'], {'b': {'undo': [], 'redo': []}, 'bytes': 0, 'active': 'b'})
        self.assertTrue(result['same'])

    def test_direct_stack_changes_can_be_reconciled_and_unicode_is_counted(self):
        result = self.run_js("""
const h = createPhotoUndoHistory({maxSteps: 3, maxBytes: 8});
const a = h.activate('a');
a.undo.push('a', 'b', 'c', '😀'); h.trim();
const before = {undo: [...a.undo], bytes: h.bytes};
h.push('😀');
const duplicate = [...a.undo];
console.log(JSON.stringify({before, duplicate}));
""")
        self.assertEqual(result['before'], {'undo': ['b', 'c', '😀'], 'bytes': 8})
        self.assertEqual(result['duplicate'], ['b', 'c', '😀'])

    def test_invalid_input_does_not_corrupt_history_and_zero_budget_disables_it(self):
        result = self.run_js("""
const errors = [];
for (const options of [{maxPhotos:0}, {maxSteps:-1}, {maxBytes:Infinity}]) {
  try { createPhotoUndoHistory(options); } catch (e) { errors.push(e.name); }
}
const h = createPhotoUndoHistory(); const a = h.activate('a'); h.push('old');
try { h.undo({}); } catch (e) { errors.push(e.name); }
try { h.push({}); } catch (e) { errors.push(e.name); }
const off = createPhotoUndoHistory({maxBytes:0}); const b = off.activate('b'); off.push('value');
console.log(JSON.stringify({errors, a, b, bytes:off.bytes, undo:off.undo('now')}));
""")
        self.assertEqual(result['errors'], ['RangeError', 'RangeError', 'RangeError', 'TypeError', 'TypeError'])
        self.assertEqual(result['a'], {'undo': ['old'], 'redo': []})
        self.assertEqual(result['b'], {'undo': [], 'redo': []})
        self.assertEqual(result['bytes'], 0)
        self.assertIsNone(result['undo'])


if __name__ == '__main__':
    unittest.main()
