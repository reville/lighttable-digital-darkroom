import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from film_lab_ai.face_models import ensure_models, valid_model
from film_lab_ai.face_service import FaceService
from film_lab_ai.face_store import FaceStore


def face(axis=0, box=None):
    vector = np.zeros(128, dtype=np.float32)
    vector[axis] = 1
    return dict(vector=vector, thumbnail=b'jpeg', quality=1,
                box=box or [.1, .1, .3, .3])


def similar_face(score, axis=1, box=None):
    result = face(box=box)
    result['vector'][0] = score
    result['vector'][axis] = np.sqrt(1 - score ** 2)
    return result


class FaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FaceStore(Path(self.temp.name) / 'faces.sqlite3')

    def add(self, name, faces=None, fingerprint='v1'):
        self.store.add_photo(name, fingerprint, 'test', faces if faces is not None else [face()])

    def test_default_reads_create_no_storage(self):
        self.assertEqual(self.store.gallery(), [])
        self.assertEqual(self.store.labels(['photo']), {})
        self.assertEqual(self.store.suggestions(), [])
        self.assertEqual(self.store.stats()['faces'], 0)
        self.assertFalse(self.store.path.exists())

    def test_similar_faces_group_but_people_together_stay_separate(self):
        self.add('one.jpg', [face(), face(box=[.6,.1,.3,.3])])
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(self.store.suggestions(), [])
        self.add('two.jpg')  # Ambiguous equally strong groups stay separate.
        self.assertEqual(self.store.stats()['groups'], 3)

    def test_strong_new_faces_join_named_group_and_inherit_searchable_name(self):
        self.add('one.jpg'); self.add('two.jpg')
        group = self.store.gallery()[0]
        self.assertEqual(group['photoCount'], 2)
        self.store.edit('cover', dict(group=group['id'], face=group['cover']))
        self.store.edit('rename', dict(group=group['id'], name='  Alice  Smith '))
        self.assertEqual(self.store.labels(['one.jpg']), {'one.jpg': ['Alice Smith']})
        self.add('three.jpg', [similar_face(.65)])
        self.assertEqual(self.store.stats()['groups'], 1)
        self.assertEqual(self.store.labels(['three.jpg']), {'three.jpg': ['Alice Smith']})
        self.assertEqual(self.store.gallery()[0]['cover'], group['cover'])
        self.assertEqual(self.store.suggestions(), [])

    def test_unnamed_threshold_groups_moderate_matches_but_keeps_weaker_ones_for_review(self):
        self.add('reference.jpg')
        group = self.store.gallery()[0]['id']
        self.add('moderate.jpg', [similar_face(.52)])
        self.assertEqual(self.store.stats()['groups'], 1)
        self.assertEqual({f['photo'] for f in self.store.members(group)}, {'reference.jpg', 'moderate.jpg'})
        # Start independently: reference averaging must not change this boundary.
        self.store = FaceStore(Path(self.temp.name) / 'weaker.sqlite3')
        self.add('reference.jpg')
        self.add('weaker.jpg', [similar_face(.48)])
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(len(self.store.suggestions()), 1)

    def test_named_groups_require_stronger_evidence_than_unnamed_groups(self):
        self.add('reference.jpg')
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Alice'))
        self.add('moderate.jpg', [similar_face(.58)])
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(self.store.labels(['moderate.jpg']), {})
        self.assertEqual(len(self.store.suggestions()), 1)

    def test_named_and_unnamed_rivals_both_prevent_ambiguous_assignments(self):
        for named_axis in (0, 1):
            with self.subTest(named_axis=named_axis):
                self.store = FaceStore(Path(self.temp.name) / f'ambiguous-{named_axis}.sqlite3')
                self.add('a.jpg', [face(axis=0)])
                self.add('b.jpg', [face(axis=1)])
                named = next(g['id'] for g in self.store.gallery()
                             if self.store.members(g['id'])[0]['photo'] == ('a.jpg' if named_axis == 0 else 'b.jpg'))
                self.store.edit('rename', dict(group=named, name='Alice'))
                candidate = face()
                candidate['vector'][:3] = [.65, .62, np.sqrt(1 - .65**2 - .62**2)]
                self.add('ambiguous.jpg', [candidate])
                self.assertEqual(self.store.stats()['groups'], 3)
                self.assertEqual(self.store.labels(['ambiguous.jpg']), {})

    def test_named_rival_below_its_threshold_still_counts_for_ambiguity(self):
        self.add('a.jpg', [face(axis=0)])
        self.add('b.jpg', [face(axis=1)])
        group = next(g['id'] for g in self.store.gallery() if self.store.members(g['id'])[0]['photo'] == 'b.jpg')
        self.store.edit('rename', dict(group=group, name='Alice'))
        candidate = face()
        candidate['vector'][:3] = [.58, .54, np.sqrt(1 - .58**2 - .54**2)]
        self.add('ambiguous.jpg', [candidate])
        self.assertEqual(self.store.stats()['groups'], 3)

    def test_named_group_requires_reference_agreement_not_one_lucky_match(self):
        self.add('a.jpg')
        self.add('b.jpg', [similar_face(.52)])
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Alice'))
        candidate = similar_face(.7)
        candidate['vector'][1] *= -1
        self.add('outlier.jpg', [candidate])
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(self.store.labels(['outlier.jpg']), {})

    def test_hidden_groups_never_receive_new_faces(self):
        self.add('a.jpg')
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Alice'))
        self.store.edit('hide', dict(group=group, hidden=True))
        self.add('b.jpg')
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(self.store.labels(['b.jpg']), {})

    def test_two_faces_in_new_photo_cannot_both_join_same_named_group(self):
        self.add('reference.jpg')
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Alice'))
        self.add('together.jpg', [face(), face(box=[.6, .1, .3, .3])])
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(sum(f['photo'] == 'together.jpg' for f in self.store.members(group)), 1)
        self.assertEqual(self.store.suggestions(), [])

    def test_split_assignments_survive_rescan_even_with_strong_named_match(self):
        self.add('a.jpg'); self.add('b.jpg')
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Alice'))
        selected = next(f['id'] for f in self.store.members(group) if f['photo'] == 'b.jpg')
        self.store.edit('split', dict(group=group, faces=[selected]))
        split_group = next(g['id'] for g in self.store.gallery() if g['id'] != group)
        self.add('b.jpg', fingerprint='v2')
        self.assertEqual(self.store.members(split_group)[0]['id'], selected)
        self.assertEqual(self.store.labels(['b.jpg']), {})
        self.assertEqual(self.store.suggestions(), [])

    def test_reject_survives_restart_and_rescan(self):
        self.add('one.jpg')
        first = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=first, name='Alice'))
        self.add('two.jpg', [similar_face(.52)])
        second = next(g['id'] for g in self.store.gallery() if g['id'] != first)
        self.store.edit('reject', dict(group=first, other=second))
        self.store = FaceStore(self.store.path)
        self.add('two.jpg', [similar_face(.52)], fingerprint='v2')
        self.assertEqual(self.store.suggestions(), [])
        self.assertEqual(next(g['id'] for g in self.store.gallery() if not g['name']), second)
        self.assertEqual(self.store.gallery()[0]['name'], 'Alice')

    def test_split_persists_and_undo_restores_assignment(self):
        self.add('one.jpg'); self.add('two.jpg')
        group = self.store.gallery()[0]['id']
        selected = self.store.members(group)[0]['id']
        self.store.edit('split', dict(group=group, faces=[selected]))
        self.assertEqual(self.store.stats()['groups'], 2)
        self.assertEqual(self.store.suggestions(), [])
        self.store.edit('undo', {})
        self.assertEqual(self.store.stats()['groups'], 1)
        self.assertEqual(len(self.store.members(group)), 2)

    def test_merge_redirects_rejections_and_undo_preserves_names(self):
        ids = []
        for n in range(3):
            self.add(f'{n}.jpg', [similar_face(np.sqrt(.5), axis=n+1)])
            g = next(g for g in self.store.gallery() if not g['name'])
            ids.append(g['id'])
            self.store.edit('rename', dict(group=g['id'], name=f'Name {n}'))
        self.store.edit('reject', dict(group=ids[1], other=ids[2]))
        self.store.edit('merge', dict(group=ids[0], other=ids[1]))
        self.assertEqual(self.store.suggestions(), [])
        self.assertEqual(self.store.labels(['1.jpg']), {'1.jpg': ['Name 0']})
        self.store.edit('undo', {})
        self.assertEqual(self.store.labels(['1.jpg']), {'1.jpg': ['Name 1']})
        self.assertEqual(self.store.stats()['groups'], 3)

    def test_hide_cover_undo_and_missing_photo_cleanup(self):
        self.add('one.jpg'); self.add('two.jpg')
        group = self.store.gallery()[0]['id']
        chosen = self.store.members(group)[1]['id']
        self.store.edit('cover', dict(group=group, face=chosen))
        self.assertEqual(self.store.gallery()[0]['cover'], chosen)
        self.store.edit('rename', dict(group=group, name='Person'))
        self.store.edit('hide', dict(group=group, hidden=True))
        self.assertEqual(self.store.labels(['one.jpg']), {})
        self.store.edit('undo', {})
        self.assertEqual(self.store.labels(['one.jpg']), {'one.jpg': ['Person']})
        self.store.remove_missing(['one.jpg'])
        self.assertEqual(self.store.stats()['photos'], 1)
        self.assertEqual(self.store.stats()['faces'], 1)
        self.assertFalse(self.store.stats()['canUndo'])

    def test_model_fingerprint_and_no_face_photos_are_reused(self):
        self.add('empty.jpg', [])
        self.assertTrue(self.store.current('empty.jpg', 'v1', 'test'))
        self.assertFalse(self.store.current('empty.jpg', 'v2', 'test'))
        self.assertFalse(self.store.current('empty.jpg', 'v1', 'new-model'))

    def test_bad_mutations_roll_back_and_keep_previous_undo(self):
        self.add('one.jpg'); self.add('two.jpg')
        group = self.store.gallery()[0]['id']
        self.store.edit('rename', dict(group=group, name='Saved name'))
        for action, values in [('merge', dict(other='missing')), ('cover', dict(face='missing')),
                               ('split', dict(faces=['missing'])), ('rename', dict(name='a'*101))]:
            with self.assertRaises(ValueError):
                self.store.edit(action, dict(group=group, **values))
        self.assertEqual(self.store.gallery()[0]['name'], 'Saved name')
        self.store.edit('undo', {})
        self.assertEqual(self.store.gallery()[0]['name'], '')

    def test_refresh_retains_cover_group_and_name(self):
        self.add('one.jpg')
        group = self.store.gallery()[0]
        self.store.edit('rename', dict(group=group['id'], name='Alice'))
        self.add('one.jpg', fingerprint='v2')
        after = self.store.gallery()[0]
        self.assertEqual(after['id'], group['id'])
        self.assertEqual(after['cover'], group['cover'])
        self.assertEqual(after['name'], 'Alice')

    def test_replacement_photo_does_not_inherit_a_person_name(self):
        self.add('one.jpg')
        group = self.store.gallery()[0]
        self.store.edit('rename', dict(group=group['id'], name='Alice'))
        self.add('one.jpg', [face(axis=1)], fingerprint='replacement')
        self.assertEqual(self.store.labels(['one.jpg']), {})
        self.assertNotEqual(self.store.gallery()[0]['id'], group['id'])

    def test_catalog_rename_and_name_swap_keep_assignments_and_corrections(self):
        for name, source_id, axis in [('one.jpg', 12, 0), ('two.jpg', 13, 1)]:
            self.store.add_photo(name, 'v1', 'test', [face(axis)], source_id)
        groups = self.store.gallery()
        first = next(g for g in groups if self.store.members(g['id'])[0]['photo'] == 'one.jpg')
        self.store.edit('rename', dict(group=first['id'], name='Alice'))
        self.store.reconcile_names({'two.jpg': 12, 'one.jpg': 13})
        self.assertEqual(self.store.labels(['one.jpg', 'two.jpg']), {'two.jpg': ['Alice']})
        self.store.reconcile_names({'folder/renamed.jpg': 12, 'one.jpg': 13})
        self.store.remove_missing(['folder/renamed.jpg', 'one.jpg'])
        self.assertEqual(self.store.labels(['folder/renamed.jpg']), {'folder/renamed.jpg': ['Alice']})
        self.assertEqual(self.store.stats()['faces'], 2)
        self.assertEqual(next(g for g in self.store.gallery() if g['id'] == first['id'])['cover'], first['cover'])

    def test_clear_removes_names_faces_and_reclaims_storage(self):
        large_face = dict(face(), thumbnail=b'x' * 500000)
        self.add('one.jpg', [large_face])
        before = self.store.path.stat().st_size
        self.store.clear()
        self.assertEqual(self.store.stats()['faces'], 0)
        self.assertEqual(self.store.suggestions(), [])
        self.assertLess(self.store.path.stat().st_size, before)


class FakeAnalyzer:
    def __init__(self):
        self.calls = []
    def capabilities(self):
        return dict(available=True)
    def prepare(self, cancelled, progress):
        pass
    def analyze(self, data):
        self.calls.append(data)
        if data == b'bad':
            raise ValueError('bad preview')
        return [face()]
    def shutdown(self):
        pass


class FaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.analyzer = FakeAnalyzer()
        self.names = ['one.jpg', 'two.jpg', 'empty.jpg']
        self.service = FaceService(library=self.root / 'library', data_root=self.root / 'data',
            list_images=lambda: self.names, source_key=lambda n: 'v1',
            preview_bytes=lambda n: n.encode(),
            source_availability=lambda n: 'empty' if n == 'empty.jpg' else 'local', analyzer=self.analyzer)
        self.addCleanup(self.service.shutdown)

    def wait(self):
        for _ in range(200):
            if not self.service.status()['running']:
                return self.service.status()
            time.sleep(.01)
        self.fail('Scan did not stop')

    def test_enable_skip_resume_and_preserve_local_labels_while_paused(self):
        self.service.start()
        self.assertFalse(self.service.root.exists())
        self.service.action(dict(action='enable'))
        status = self.wait()
        self.assertEqual((status['photos'], status['skipped'], status['errors']), (2, 1, 0))
        self.service.action(dict(action='scan')); self.wait()
        self.assertEqual(len(self.analyzer.calls), 2)
        group = self.service.store.gallery()[0]['id']
        self.service.action(dict(action='rename', group=group, name='Alice'))
        self.service.action(dict(action='disable'))
        self.assertEqual(self.service.labels(['one.jpg']), {'one.jpg':['Alice']})

    def test_incremental_scan_extends_named_group_without_reanalyzing_existing_photos(self):
        self.service.action(dict(action='enable'))
        self.wait()
        group = self.service.store.gallery()[0]
        self.service.action(dict(action='cover', group=group['id'], face=group['cover']))
        self.service.action(dict(action='rename', group=group['id'], name='Alice'))
        self.names.append('new.jpg')
        self.service.action(dict(action='scan'))
        status = self.wait()
        self.assertEqual((status['photos'], status['groups'], status['errors']), (3, 1, 0))
        self.assertEqual(self.analyzer.calls, [b'one.jpg', b'two.jpg', b'new.jpg'])
        self.assertEqual(self.service.labels(['new.jpg']), {'new.jpg': ['Alice']})
        self.assertEqual(self.service.store.gallery()[0]['cover'], group['cover'])
        self.assertEqual(self.service.store.suggestions(), [])

    def test_clear_during_analysis_does_not_repopulate(self):
        entered, release = threading.Event(), threading.Event()
        def block(data):
            entered.set(); release.wait(3); return [face()]
        self.analyzer.analyze = block
        self.service.action(dict(action='enable'))
        self.assertTrue(entered.wait(2))
        self.service.action(dict(action='clear'))
        release.set(); self.wait()
        self.assertEqual(self.service.store.stats()['faces'], 0)

    def test_bad_photo_does_not_stop_scan(self):
        self.names[:] = ['bad', 'good']
        self.service.action(dict(action='enable'))
        status = self.wait()
        self.assertEqual((status['errors'], status['photos']), (1, 1))
        self.assertTrue(status['scanComplete'])


class DownloadTests(unittest.TestCase):
    def test_bad_download_is_not_published_and_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
            root = Path(d)
            with self.assertRaises(RuntimeError):
                ensure_models(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_cancel_does_not_publish(self):
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
            with self.assertRaises(InterruptedError):
                ensure_models(Path(d), cancelled=lambda: True)
            self.assertEqual(list(Path(d).iterdir()), [])
