# SPDX-License-Identifier: GPL-3.0-only
"""Clock corrections preserve originals, scan identity, per-file history and export metadata."""
from datetime import datetime
import json
from pathlib import Path
from unittest import mock
import unittest

import capture_time
import catalog as catalog_module
import catalog_scan
import platform_image
import server
import xmp_sidecar
from tests.test_server_catalog import CatalogServerTestCase


class ClockMathTests(unittest.TestCase):
    def test_offset_and_zone_are_separate_and_midnight_leap_days_work(self):
        self.assertEqual(capture_time.corrected_timestamp('2024:02:28 23:30:00', 3600, '+05:30'),
                         '2024-02-29T00:30:00+05:30')
        self.assertEqual(capture_time.corrected_timestamp('2026-01-01T00:15:00-04:00', -3600),
                         '2025-12-31T23:15:00-04:00')
        self.assertEqual(capture_time.corrected_timestamp('2026-01-01T12:00:00-04:00', 0, '+02:00'),
                         '2026-01-01T12:00:00+02:00')
        self.assertEqual(capture_time.corrected_timestamp('2026-01-01T12:00:00-04:00', 0, 'remove'),
                         '2026-01-01T12:00:00')

    def test_invalid_values_are_rejected_without_guessing(self):
        for stamp in ('', '2026-01-01', '2026-02-30T12:00:00', 'yesterday'):
            with self.assertRaises(ValueError): capture_time.parse_timestamp(stamp)
        for zone in ('EST', '+15:00', '+05:99', '5:00'):
            with self.assertRaises(ValueError): capture_time.corrected_timestamp('2026-01-01T00:00:00', 0, zone)
        with self.assertRaises(ValueError): capture_time.corrected_timestamp('2026-01-01T00:00:00', float('nan'))


class CaptureWorkflowTests(CatalogServerTestCase):
    def setUp(self):
        super().setUp()
        self.names = [self.qualified('a.jpg'), self.qualified('sub/b.jpg')]
        self.dates = dict(zip(self.names, ['2026:01:01 23:30:00', '2026:01:01 23:35:00']))
        with self.catalog.write() as conn:
            for name, stamp in self.dates.items():
                info=self.catalog.capture_details(server.catalog_image_id(name))
                conn.execute('UPDATE files SET capture_time=? WHERE id=?',
                             (capture_time.normalized_timestamp(stamp), info['fileId']))
        self.patches=[mock.patch.object(server, 'exif_for', lambda name: {'DateTimeOriginal': self.dates.get(name, '')}),
                      mock.patch.object(server, '_queue_mirror'), mock.patch.object(server, 'queue_sidecar'),
                      mock.patch.object(server.EVENTS, 'publish')]
        for patch in self.patches: patch.start()

    def tearDown(self):
        for patch in self.patches: patch.stop()
        super().tearDown()

    def plan(self, **values):
        return server.capture_time_action({'action':'preview','names':self.names,'shiftSeconds':3600,**values})

    def apply(self, plan):
        return server.capture_time_action({'action':'apply','changes':plan['changes']})

    def test_catalog_date_fallback_discloses_missing_camera_capture_time(self):
        with mock.patch.object(server, 'exif_for', return_value={}):
            plan = self.plan()
        self.assertEqual(plan['count'], 2)
        self.assertIn('image modification date', plan['changes'][0]['warning'])
        self.assertFalse(self.catalog.capture_details(server.catalog_image_id(self.names[0]))['override'])

    def test_preview_is_read_only_then_apply_preserves_intervals_and_files(self):
        originals={name:(server.src_path(name).read_bytes(),server.src_path(name).stat().st_mtime_ns) for name in self.names}
        plan=self.plan(timeZone='+05:30')
        self.assertEqual(plan['count'],2)
        self.assertEqual(plan['changes'][0]['after'],'2026-01-02T00:30:00+05:30')
        self.assertIsNone(self.catalog.capture_details(server.catalog_image_id(self.names[0]))['override'])
        self.apply(plan)
        rows=self.catalog.query({'limit':10})['items']
        updated={item['name']:item['captureTime'] for item in rows}
        first,second=map(lambda name:datetime.fromisoformat(updated[name]),self.names)
        self.assertEqual((second-first).total_seconds(),300)
        for name,(pixels,mtime) in originals.items():
            self.assertEqual(server.src_path(name).read_bytes(),pixels)
            self.assertEqual(server.src_path(name).stat().st_mtime_ns,mtime)
        self.assertEqual(server.export_metadata_fields(self.names[0])['captureTime'],'2026-01-02T00:30:00+05:30')

    def test_scan_and_virtual_copy_share_durable_file_override(self):
        image_id=server.catalog_image_id(self.names[0])
        copy_id=self.catalog.add_virtual_copy(image_id,'copy','Virtual')
        plan=self.plan(); self.apply(plan)
        corrected=self.catalog.capture_details(image_id)['override']
        self.assertEqual(self.catalog.capture_details(copy_id)['override'],corrected)
        catalog_scan.scan_source(self.catalog,self.source,read_metadata_for_new=False)
        self.assertEqual(self.catalog.capture_details(image_id)['override'],corrected)
        self.assertEqual(self.catalog.capture_details(image_id)['original'],'2026-01-01T23:30:00')

    def test_companion_inclusion_is_explicit_and_virtual_selection_deduplicates_file(self):
        (self.root/'a.CR3').write_bytes(b'RAW fixture')
        catalog_scan.scan_source(self.catalog,self.source,read_metadata_for_new=False)
        raw=self.qualified('a.CR3'); self.dates[raw]='2026:01:01 23:30:00'
        source_id=server.catalog_image_id(self.names[0])
        self.catalog.add_virtual_copy(source_id,'alternate','Alternate')
        virtual=catalog_module.qualified_name(self.source,'a.jpg','alternate')
        one=self.plan(names=[self.names[0],virtual])
        self.assertEqual(one['count'],1)
        paired=self.plan(names=[self.names[0]],includePairs=True)
        self.assertEqual(paired['count'],2)
        self.assertEqual({item['name'] for item in paired['changes']},{self.names[0],raw})

    def test_stale_preview_rejects_whole_batch_and_wrong_identity_is_refused(self):
        plan=self.plan()
        self.catalog.set_capture_override(server.catalog_image_id(self.names[1]),'2026-02-01T12:00:00')
        with self.assertRaisesRegex(ValueError,'changed since preview'): self.apply(plan)
        self.assertIsNone(self.catalog.capture_details(server.catalog_image_id(self.names[0]))['override'])
        plan=self.plan(); plan['changes'][0]['fileId']=99999
        with self.assertRaisesRegex(ValueError,'identity changed'): self.apply(plan)

    def test_reset_and_history_restore_do_not_revert_later_pixel_edits(self):
        image_id=server.catalog_image_id(self.names[0]); self.apply(self.plan())
        history=self.catalog.history_for(image_id)
        before=next(step for step in history if step['label']=='Before capture time correction')
        after=next(step for step in history if step['label']=='Capture time corrected')
        server.save_image_state(self.names[0],{'grade':{'exposure':2},'rating':5})
        server.capture_time_action({'action':'restore-history','name':self.names[0],'historyId':before['id']})
        self.assertIsNone(self.catalog.capture_details(image_id)['override'])
        self.assertEqual(self.catalog.state_for(image_id)['grade'],{'exposure':2})
        self.assertEqual(self.catalog.state_for(image_id)['rating'],5)
        server.capture_time_action({'action':'restore-history','name':self.names[0],'historyId':after['id']})
        self.assertEqual(self.catalog.capture_details(image_id)['override'],'2026-01-02T00:30:00')
        with self.assertRaisesRegex(ValueError,'does not belong'):
            server.capture_time_action({'action':'restore-history','name':self.names[1],'historyId':after['id']})
        self.apply(self.plan(reset=True))
        self.assertIsNone(self.catalog.capture_details(image_id)['override'])

    def test_history_and_override_are_atomic_if_history_storage_fails(self):
        # A real SQLite trigger simulates a failing history insert mid-batch.
        with self.catalog.write() as conn:
            conn.execute("CREATE TEMP TRIGGER fail_capture_history BEFORE INSERT ON history WHEN NEW.image_id="
                         +str(server.catalog_image_id(self.names[1]))+" BEGIN SELECT RAISE(FAIL,'disk failure'); END")
        with self.assertRaisesRegex(Exception,'disk failure'): self.apply(self.plan())
        for name in self.names:
            ident=server.catalog_image_id(name)
            self.assertIsNone(self.catalog.capture_details(ident)['override'])
            self.assertEqual(self.catalog.history_for(ident),[])

    def test_portable_mirror_and_xmp_keep_capture_override(self):
        self.apply(self.plan(timeZone='-04:00'))
        self.assertTrue(catalog_scan.mirror_state_file(self.catalog,self.source))
        state=json.loads((self.root/catalog_scan.STATE_FILENAME).read_text())
        stamp=state['images']['a.jpg']['captureTimeOverride']
        self.assertEqual(stamp,'2026-01-02T00:30:00-04:00')
        self.assertEqual(xmp_sidecar.parse(xmp_sidecar.build_sidecar(state['images']['a.jpg']))['captureTime'],stamp)
        fresh=catalog_module.Catalog(Path(self._dir.name)/'other.sqlite3')
        try:
            ident=fresh.add_source(self.root); catalog_scan.scan_source(fresh,ident,read_metadata_for_new=False)
            catalog_scan.import_state_file(fresh,ident)
            self.assertEqual(fresh.capture_details(fresh.image_id_for(ident,'a.jpg'))['override'],stamp)
        finally: fresh.close()

    def test_missing_dates_are_skipped_without_using_filesystem_time(self):
        with self.catalog.write() as conn: conn.execute('UPDATE files SET capture_time=NULL')
        self.dates={}
        result=self.plan()
        self.assertEqual(result['count'],0); self.assertEqual(len(result['skipped']),2)

    def test_source_offsets_are_kept_and_sql_sort_compares_instants(self):
        self.catalog.set_capture_override(server.catalog_image_id(self.names[0]),'2026-01-01T12:00:00+05:00')
        self.catalog.set_capture_override(server.catalog_image_id(self.names[1]),'2026-01-01T09:00:00+00:00')
        rows=self.catalog.query({'sort':{'field':'capture','direction':'asc'},'limit':10})['items']
        self.assertEqual([row['name'] for row in rows],self.names)

    def test_info_cache_keeps_original_metadata_and_refreshes_override_and_reset(self):
        self.patches[0].stop()
        name=self.names[0]; image_id=server.catalog_image_id(name)
        original={'DateTimeOriginal':'2026:01:01 23:30:00','OffsetTimeOriginal':'-04:00'}
        with mock.patch.dict(server._EXIF_CACHE,clear=True), \
                mock.patch.object(platform_image,'metadata',return_value=original) as read:
            self.assertEqual(server.exif_for(name),original)
            self.catalog.set_capture_override(image_id,'2026-01-02T00:30:00+05:30')
            self.assertEqual(server.exif_for(name)['DateTimeOriginal'],'2026:01:02 00:30:00')
            self.assertEqual(server.exif_for(name)['OffsetTimeOriginal'],'+05:30')
            self.assertEqual(server._EXIF_CACHE[name],original)
            self.catalog.set_capture_override(image_id,None)
            self.assertEqual(server.exif_for(name),original)
            read.assert_called_once()
