"""Crash detection, photo quarantine, catalog verify/repair/salvage, and the
recovery decisions the server offers instead of making them on its own."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import catalog as catalog_module
import durable_io
import recovery
import server


def seeded_catalog(directory: Path, *, photos: int = 300,
                   history: int = 300) -> catalog_module.Catalog:
    """A catalog with enough rows to span many pages."""
    cat = catalog_module.Catalog(directory / "library.sqlite3")
    source_id = cat.add_source(directory)
    with cat.write() as conn:
        for index in range(photos):
            cat.upsert_file(conn, source_id, {
                "relpath": f"f{index}.jpg", "filename": f"f{index}.jpg",
                "ext": ".jpg", "size": 1000 + index, "mtime_ns": index,
                "header_hash": f"hash{index:06d}",
            })
    for image_id in range(1, history + 1):
        cat.add_history(image_id, "edit",
                        {"grade": {"exposure": image_id / 100}, "pad": "x" * 3000})
    cat.save_state(1, {"rating": 4, "grade": {"exposure": 0.5}})
    cat.checkpoint()
    return cat


def damage_table_pages(path: Path, table: str = "history") -> None:
    """Overwrite the pages holding one table so its rows cannot be read.

    Other tables stay intact, which is what a bad sector or a torn write
    looks like and what makes a partial salvage worth offering.
    """
    conn = sqlite3.connect(path)
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    try:
        pages = [int(row[0]) for row in conn.execute(
            "SELECT pageno FROM dbstat WHERE name=?", (table,))]
    except sqlite3.Error:
        pages = []
    conn.close()
    if not pages:
        # Without dbstat, fall back to the tail of the file, where the
        # rows written last usually live.
        size = path.stat().st_size
        pages = [size // page - 2, size // page - 1, size // page]
    with path.open("r+b") as handle:
        for number in pages:
            handle.seek((number - 1) * page)
            handle.write(b"\xa5" * page)


class StartupReporterTests(unittest.TestCase):
    def test_phases_and_failure_are_written_for_the_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.json"
            reporter = recovery.StartupReporter(path)
            reporter.phase("catalog", "Opening the catalog…")
            self.assertEqual(json.loads(path.read_text())["phase"], "catalog")
            reporter.failed("catalog-locked", "held elsewhere",
                            hint="quit it", holder={"pid": 42})
            record = json.loads(path.read_text())
            self.assertEqual(record["phase"], "failed")
            self.assertEqual(record["code"], "catalog-locked")
            self.assertEqual(record["holder"], {"pid": 42})
            reporter.ready(8321)
            self.assertEqual(json.loads(path.read_text())["port"], 8321)
            reporter.remove()
            self.assertFalse(path.exists())

    def test_a_reporter_without_a_path_writes_nothing(self):
        reporter = recovery.StartupReporter(None)
        reporter.phase("starting")
        reporter.failed("x", "y")
        self.assertEqual(reporter.record["phase"], "failed")


class SessionLedgerTests(unittest.TestCase):
    def test_a_clean_quit_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.SessionLedger(directory)
            self.assertIsNone(ledger.begin(port=1))
            ledger.end()
            again = recovery.SessionLedger(directory)
            self.assertIsNone(again.begin(port=2))
            self.assertEqual(again.crash_count(3600), 0)

    def test_an_unended_session_is_recorded_as_a_crash_with_its_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.SessionLedger(directory)
            ledger.begin(port=1)
            marker = ledger.inflight("render", "1:trip/frame.RAF")
            marker.__enter__()  # the process dies before this ever exits
            self.assertTrue(ledger.inflight_path.exists())
            relaunch = recovery.SessionLedger(directory)
            crashed = relaunch.begin(port=2, previous_exit="-11")
            self.assertIsNotNone(crashed)
            self.assertEqual(crashed["inflight"]["name"], "1:trip/frame.RAF")
            self.assertEqual(crashed["inflight"]["stage"], "render")
            self.assertEqual(crashed["exitStatus"], "-11")
            self.assertEqual(relaunch.crash_count(3600), 1)
            self.assertFalse(relaunch.inflight_path.exists(),
                             "a new session starts with no blame attached")
            summary = relaunch.summary()
            self.assertEqual(summary["crashes24h"], 1)
            self.assertTrue(summary["previousCrash"])

    def test_the_marker_is_cleared_shortly_after_work_completes(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(recovery, "INFLIGHT_SETTLE_SECONDS", 0.05):
            ledger = recovery.SessionLedger(directory)
            ledger.begin(port=1)
            with ledger.inflight("render", "a.jpg"):
                with ledger.inflight("decode", "a.jpg"):
                    self.assertTrue(ledger.inflight_path.exists())
                # Still inside the outer stage: the nested exit must not clear.
                self.assertTrue(ledger.inflight_path.exists())
            deadline = time.time() + 2
            while ledger.inflight_path.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(ledger.inflight_path.exists())

    def test_a_vanished_launcher_is_not_counted_as_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.SessionLedger(directory)
            ledger.begin(port=1)
            ledger.end("parent-gone")
            self.assertIsNone(recovery.SessionLedger(directory).begin(port=2))

    def test_inflight_is_inert_before_a_session_begins(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.SessionLedger(directory)
            with ledger.inflight("render", "a.jpg"):
                pass
            self.assertFalse(ledger.inflight_path.exists())


class PhotoQuarantineTests(unittest.TestCase):
    def test_two_strikes_set_a_photo_aside_and_release_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            quarantine = recovery.PhotoQuarantine(Path(directory) / "q.json")
            first = quarantine.strike("a.RAF", "decode")
            self.assertFalse(first["quarantined"])
            self.assertFalse(quarantine.is_quarantined("a.RAF"))
            second = quarantine.strike("a.RAF", "decode")
            self.assertTrue(second["quarantined"])
            self.assertTrue(quarantine.is_quarantined("a.RAF"))
            reloaded = recovery.PhotoQuarantine(Path(directory) / "q.json")
            self.assertEqual(reloaded.quarantined(), ["a.RAF"])
            self.assertTrue(reloaded.release("a.RAF"))
            self.assertFalse(reloaded.release("a.RAF"))
            self.assertEqual(reloaded.entries(), [])

    def test_a_previous_crash_strikes_the_photo_it_was_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            quarantine = recovery.PhotoQuarantine(Path(directory) / "q.json")
            entry = quarantine.note_previous_crash(
                {"inflight": {"stage": "render", "name": "b.RAF"},
                 "detectedAt": 1000.0})
            self.assertEqual((entry["name"], entry["strikes"], entry["lastAt"]),
                             ("b.RAF", 1, 1000.0))
            self.assertIsNone(quarantine.note_previous_crash(
                {"inflight": None}))
            self.assertIsNone(quarantine.note_previous_crash(None))

    def test_multi_worker_crash_strikes_all_active_photos(self):
        with tempfile.TemporaryDirectory() as directory:
            quarantine = recovery.PhotoQuarantine(Path(directory) / "q.json")
            entry = quarantine.note_previous_crash({
                "inflight": {
                    "active": [
                        {"stage": "export", "name": "bad.raw"},
                        {"stage": "export", "name": "innocent.raw"},
                    ]
                },
                "detectedAt": 2000.0,
            })
            self.assertIsNotNone(entry)
            entries = {item["name"]: item for item in quarantine.entries()}
            self.assertIn("bad.raw", entries)
            self.assertIn("innocent.raw", entries)
            self.assertEqual(entries["bad.raw"]["strikes"], 1)
            self.assertEqual(entries["innocent.raw"]["strikes"], 1)

    def test_concurrent_inflight_workers_are_tracked_together(self):
        import concurrent.futures
        import threading
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.SessionLedger(directory)
            ledger.begin(port=1)
            t1_ready = threading.Event()
            t2_ready = threading.Event()
            stop = threading.Event()

            def worker1():
                with ledger.inflight("export", "worker1.raw"):
                    t1_ready.set()
                    stop.wait(5)

            def worker2():
                with ledger.inflight("export", "worker2.raw"):
                    t2_ready.set()
                    stop.wait(5)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(worker1)
                f2 = pool.submit(worker2)
                t1_ready.wait(2)
                t2_ready.wait(2)
                data = durable_io.load_json(ledger.inflight_path, {})
                self.assertIsNotNone(data)
                active_names = {item["name"] for item in data.get("active", [])}
                self.assertEqual(active_names, {"worker1.raw", "worker2.raw"})
                stop.set()
                f1.result()
                f2.result()

    def test_a_damaged_quarantine_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "q.json"
            path.write_text("{not json")
            self.assertEqual(recovery.PhotoQuarantine(path).entries(), [])


class DamagedFileCustodyTests(unittest.TestCase):
    def test_set_aside_moves_sqlite_siblings_and_records_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in ("", "-wal", "-shm"):
                (root / f"index.sqlite3{suffix}").write_bytes(b"x")
            folder = recovery.set_aside(root / "index.sqlite3", root,
                                        reason="integrity check failed")
            self.assertTrue(folder.is_dir())
            self.assertEqual(
                sorted(item.name for item in folder.iterdir()),
                ["REASON.txt", "index.sqlite3", "index.sqlite3-shm",
                 "index.sqlite3-wal"])
            self.assertFalse((root / "index.sqlite3").exists())
            self.assertIn("integrity check failed",
                          (folder / "REASON.txt").read_text())
            self.assertIsNone(recovery.set_aside(root / "missing", root,
                                                 reason="x"))

    def test_preserve_damaged_json_keeps_unreadable_bytes_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prefs.json"
            path.write_text('{"ok": true}')
            self.assertIsNone(recovery.preserve_damaged_json(path))
            self.assertTrue(path.exists())
            path.write_text('{"ok": tru')
            folder = recovery.preserve_damaged_json(path)
            self.assertIsNotNone(folder)
            self.assertFalse(path.exists())
            self.assertEqual((folder / "prefs.json").read_text(), '{"ok": tru')

    def test_inspect_json_names_which_copy_is_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prefs.json"
            self.assertEqual(recovery.inspect_json(path)["status"], "missing")
            durable_io.atomic_write_json(path, {"a": 1})
            self.assertEqual(recovery.inspect_json(path)["status"], "ok")
            durable_io.atomic_write_json(path, {"a": 2})
            path.write_text("{broken")
            self.assertEqual(recovery.inspect_json(path)["status"], "backup")
            durable_io.backup_path(path).write_text("{broken too")
            self.assertEqual(recovery.inspect_json(path)["status"], "damaged")


class EnvironmentTests(unittest.TestCase):
    def test_disk_status_walks_up_to_an_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            status = recovery.disk_status(Path(directory) / "not" / "yet")
            self.assertGreater(status["totalBytes"], 0)
            self.assertIn("low", status)

    def test_sync_services_are_recognised_from_the_path(self):
        self.assertEqual(recovery.sync_service_for(
            "/Users/example/Dropbox/Catalog/library.sqlite3"), "Dropbox")
        self.assertEqual(recovery.sync_service_for(
            "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/c.sqlite3"),
            "iCloud Drive")
        self.assertIsNone(recovery.sync_service_for(
            "/Users/x/Library/Application Support/LightTable/Catalog/l.sqlite3"))

    def test_log_rotation_keeps_generations_and_tail_reads_the_end(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "server.log"
            for launch in range(4):
                recovery.rotate_log(log)
                log.write_text(f"launch {launch}\n" + "line\n" * 200)
            names = sorted(item.name for item in Path(directory).iterdir())
            self.assertEqual(names, ["server.1.log", "server.2.log", "server.log"])
            self.assertEqual((Path(directory) / "server.1.log")
                             .read_text().splitlines()[0], "launch 2")
            self.assertEqual(recovery.log_tail(log, 3), ["line", "line", "line"])
            self.assertEqual(recovery.log_tail(None), [])


class CatalogHealthTests(unittest.TestCase):
    def test_verify_is_clean_on_a_healthy_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            cat = seeded_catalog(Path(directory), photos=20, history=5)
            report = cat.verify(full=True)
            self.assertTrue(report["ok"], report["problems"])
            self.assertFalse(report["repairable"])
            self.assertEqual(report["integrity"], "ok")
            self.assertTrue(report["searchIndex"]["ok"])
            self.assertTrue(cat.quick_check_ok())
            cat.close()

    def test_verify_finds_orphans_and_a_stale_search_index_and_repair_fixes_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=20, history=5)
            cat.close()
            # Damage the way a crash between statements could: rows whose
            # parents are gone, and search entries for photos that are not.
            raw = sqlite3.connect(root / "library.sqlite3")
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("INSERT INTO image_state(image_id, updated_at)"
                        " VALUES(9999, 0)")
            raw.execute("INSERT INTO history(image_id, seq, created, label,"
                        " state_blob) VALUES(9998, 1, 0, 'x', X'00')")
            raw.execute("INSERT INTO image_search(image_id, filename) "
                        "VALUES(7777, 'ghost.jpg')")
            raw.execute("DELETE FROM image_search WHERE image_id=1")
            raw.commit()
            raw.close()
            cat = catalog_module.Catalog(root / "library.sqlite3")
            report = cat.verify(full=True)
            self.assertFalse(report["ok"])
            self.assertTrue(report["repairable"])
            self.assertEqual(report["orphans"]["image_state"], 1)
            self.assertEqual(report["orphans"]["history"], 1)
            self.assertEqual(report["searchIndex"]["stale"], 1)
            self.assertEqual(report["searchIndex"]["unindexed"], 1)
            repaired = cat.repair(backup_directory=root / "backups")
            self.assertTrue(Path(repaired["backup"]).is_file(),
                            "repair takes a verified backup first")
            self.assertEqual(repaired["removedOrphans"]["image_state"], 1)
            self.assertEqual(repaired["removedOrphans"]["history"], 1)
            self.assertEqual(repaired["reindexed"], 20)
            self.assertTrue(repaired["verify"]["ok"])
            self.assertTrue(cat.verify(full=True)["ok"])
            cat.close()

    def test_checkpoint_folds_the_log_into_the_main_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=50, history=50)
            cat.add_history(1, "more", {"grade": {"exposure": 1}})
            result = cat.checkpoint()
            self.assertTrue(result["ok"])
            wal = Path(str(cat.path) + "-wal")
            self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
            cat.close()

    def test_a_retired_catalog_refuses_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            cat = seeded_catalog(Path(directory), photos=2, history=0)
            cat.retire()
            with self.assertRaises(RuntimeError):
                cat.stats()
            with self.assertRaises(RuntimeError):
                with cat.write():
                    pass


class SalvageTests(unittest.TestCase):
    def test_readable_rows_are_carried_into_a_fresh_verified_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root)
            cat.close()
            damage_table_pages(root / "library.sqlite3")
            reopened = catalog_module.Catalog(root / "library.sqlite3")
            self.assertFalse(reopened.quick_check_ok())
            reopened.close()
            report = catalog_module.salvage(root / "library.sqlite3",
                                            root / "salvaged.sqlite3")
            self.assertTrue(report["ok"], report)
            self.assertFalse(report["complete"])
            self.assertTrue(report["errors"], "damaged tables are named")
            self.assertEqual(report["counts"]["sources"], 1)
            self.assertGreater(report["counts"]["images"], 0)
            self.assertTrue(catalog_module._catalog_file_ok(
                root / "salvaged.sqlite3"))
            fresh = catalog_module.Catalog(root / "salvaged.sqlite3")
            self.assertTrue(fresh.verify(full=True)["ok"])
            self.assertEqual(fresh.stats()["sources"], 1)
            fresh.close()
            # The damaged file was read, never written.
            self.assertFalse(catalog_module.Catalog(
                root / "library.sqlite3").quick_check_ok())

    def test_an_unreadable_file_reports_the_open_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "library.sqlite3").write_bytes(b"garbage" * 1000)
            report = catalog_module.salvage(root / "library.sqlite3",
                                            root / "out.sqlite3")
            self.assertFalse(report["ok"])
            self.assertIn("open", report["errors"])

    def test_a_newer_schema_is_refused_rather_than_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=2, history=0)
            with cat.write() as conn:
                conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
            cat.close()
            with self.assertRaises(catalog_module.CatalogVersionError):
                catalog_module.salvage(root / "library.sqlite3", root / "o.sqlite3")

    def test_rebuild_installs_the_salvage_and_quarantines_the_damaged_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root)
            cat.close()
            damage_table_pages(root / "library.sqlite3")
            report = catalog_module.rebuild_catalog(root / "library.sqlite3")
            self.assertTrue(report["ok"])
            quarantine = Path(report["quarantine"])
            self.assertTrue((quarantine / "library.sqlite3").is_file())
            self.assertIn("before-rebuild", quarantine.name)
            installed = catalog_module.Catalog(root / "library.sqlite3")
            self.assertTrue(installed.quick_check_ok())
            self.assertTrue(installed.verify(full=True)["ok"])
            installed.close()


class BackupChoiceTests(unittest.TestCase):
    def test_backups_are_listed_newest_first_with_their_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=7, history=3)
            backups = root / "backups"
            first = cat.backup(backups)
            os.utime(first, (time.time() - 100, time.time() - 100))
            cat.add_history(1, "later", {"grade": {"exposure": 1}})
            second = cat.backup(backups)
            items = catalog_module.list_backups(backups)
            self.assertEqual([item["path"] for item in items],
                             [str(second), str(first)])
            self.assertEqual(items[0]["summary"]["images"], 7)
            self.assertEqual(items[0]["summary"]["history"], 4)
            self.assertEqual(items[1]["summary"]["history"], 3)
            self.assertEqual(items[0]["summary"]["edited"], 1)
            cat.close()

    def test_restore_installs_the_chosen_backup_and_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=3, history=2)
            backups = root / "backups"
            archive = cat.backup(backups)
            cat.add_history(1, "after backup", {"grade": {"exposure": 1}})
            cat.close()
            result = catalog_module.restore_backup(root / "library.sqlite3",
                                                   archive)
            restored = catalog_module.Catalog(root / "library.sqlite3")
            self.assertEqual(len(restored.history_for(1)), 1)
            restored.close()
            kept = catalog_module.Catalog(
                Path(result["quarantine"]) / "library.sqlite3")
            self.assertEqual(len(kept.history_for(1)), 2,
                             "the replaced catalog is preserved intact")
            kept.close()

    def test_restore_refuses_a_bad_archive_without_touching_the_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=3, history=1)
            cat.close()
            bad = root / "LightTable-catalog-2026-01-01-000000-000.zip"
            bad.write_bytes(b"not a zip")
            before = (root / "library.sqlite3").read_bytes()
            with self.assertRaises(ValueError):
                catalog_module.restore_backup(root / "library.sqlite3", bad)
            self.assertEqual((root / "library.sqlite3").read_bytes(), before)
            with self.assertRaises(FileNotFoundError):
                catalog_module.restore_backup(root / "library.sqlite3",
                                              root / "missing.zip")

    def test_a_fresh_catalog_sets_the_old_one_aside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=3, history=1)
            cat.close()
            result = catalog_module.create_fresh_catalog(root / "library.sqlite3")
            fresh = catalog_module.Catalog(root / "library.sqlite3")
            self.assertEqual(fresh.stats()["images"], 0)
            fresh.close()
            self.assertTrue((Path(result["quarantine"]) / "library.sqlite3").is_file())

    def test_tiered_pruning_keeps_a_history_that_reaches_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cat = seeded_catalog(root, photos=1, history=0)
            backups = root / "backups"
            backups.mkdir()
            # Pruning buckets by calendar day, ISO week, and month, so the
            # hour offsets below only land in distinct buckets from a fixed
            # mid-afternoon reference. Against the real clock this failed
            # before 10:00 (the 7 h and 30 h archives shared "yesterday")
            # and whenever two monthly archives fell in one month.
            reference = datetime(2026, 9, 4, 15, 0, 0)

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return reference if tz is None else reference.astimezone(tz)

            now = reference.timestamp()
            ages_hours = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10,  # ten from today
                          30, 54, 78, 102, 126,          # one a day, days 1-5
                          24 * 12, 24 * 20, 24 * 27,     # weeks 2-4
                          24 * 45, 24 * 75, 24 * 100,    # months 2-4
                          24 * 400]                      # over a year old
            for index, age in enumerate(ages_hours):
                archive = backups / f"LightTable-catalog-a{index:03d}.zip"
                archive.write_bytes(b"z")
                stamp = now - age * 3600
                os.utime(archive, (stamp, stamp))
            with mock.patch.object(catalog_module, "datetime", FrozenDateTime):
                removed = cat.prune_backups(backups, keep=4)
            remaining = sorted(backups.glob("LightTable-catalog-*.zip"),
                               key=lambda p: -p.stat().st_mtime)
            ages = [round((now - p.stat().st_mtime) / 3600) for p in remaining]
            self.assertEqual(ages[:4], [1, 2, 3, 4], "the newest four stay")
            self.assertNotIn(5, ages, "same-day extras beyond keep go")
            for daily in (30, 54, 78, 102, 126):
                self.assertIn(daily, ages)
            for weekly in (24 * 12, 24 * 20, 24 * 27):
                self.assertIn(weekly, ages)
            for monthly in (24 * 45, 24 * 75, 24 * 100):
                self.assertIn(monthly, ages)
            self.assertNotIn(24 * 400, ages, "older than every tier")
            self.assertEqual(removed, len(ages_hours) - len(ages))
            cat.close()


class ServerRecoveryTests(unittest.TestCase):
    """The server's policy: report damage and offer choices; never decide."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.catalog_path = self.root / "Catalog" / "library.sqlite3"
        self.catalog_path.parent.mkdir()
        self.prefs = self.root / "prefs.json"
        self._env = mock.patch.dict(os.environ, {
            "LIGHTTABLE_CATALOG_FILE": str(self.catalog_path),
        })
        self._env.start()
        self._patches = [
            mock.patch.object(server, "FOLDER", self.photos),
            mock.patch.object(server, "CATALOG", None),
            mock.patch.object(server, "SCANNER", None),
            mock.patch.object(server, "CATALOG_NOTICE", None),
            mock.patch.object(server, "PRIMARY_SOURCE_ID", None),
            mock.patch.object(server, "CATALOG_ENABLED", True),
            mock.patch.object(server, "AUTO_RECOVER", False),
            mock.patch.object(server, "PREFS_FILE", self.prefs),
            mock.patch.object(server, "PRESETS_FILE", self.root / "presets.json"),
            mock.patch.object(server, "STARTUP", recovery.StartupReporter(None)),
            mock.patch.object(server, "SESSION",
                              recovery.SessionLedger(self.catalog_path.parent)),
            mock.patch.object(server, "PHOTO_QUARANTINE",
                              recovery.PhotoQuarantine(
                                  self.catalog_path.parent / "q.json")),
            mock.patch.object(server, "HEALTH_STATE",
                              {"verify": None, "maintenance": None, "disk": None}),
            mock.patch.object(server, "request_restart"),
            mock.patch.object(server, "WATCH_SERVICE", None),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        if server.CATALOG is not None:
            server.CATALOG.close()
        for patch in self._patches:
            patch.stop()
        self._env.stop()
        self._dir.cleanup()

    def seed(self, **kwargs) -> catalog_module.Catalog:
        cat = seeded_catalog(self.catalog_path.parent, **kwargs)
        return cat

    def test_a_damaged_catalog_opens_folder_mode_and_asks(self):
        cat = self.seed()
        cat.backup(self.catalog_path.parent / "Backups")
        cat.close()
        damage_table_pages(self.catalog_path)
        self.assertIsNone(server.open_catalog())
        notice = server.CATALOG_NOTICE
        self.assertEqual(notice["status"], "damaged")
        self.assertEqual(notice["backups"], 1)
        self.assertIn("Library Health", notice["recovery"])
        self.assertFalse(catalog_module.Catalog(self.catalog_path).quick_check_ok(),
                         "the damaged file is left exactly as it was")
        status = server.recovery_status()
        self.assertTrue(status["catalog"]["damaged"])
        self.assertTrue(status["salvage"]["available"])
        self.assertEqual(status["salvage"]["readable"]["sources"], 1)
        self.assertEqual(len(status["backups"]["items"]), 1)
        self.assertEqual(server.library_health_summary()["status"], "damaged")

    def test_headless_servers_may_still_restore_automatically(self):
        cat = self.seed(photos=4, history=1)
        cat.backup(self.catalog_path.parent / "Backups")
        cat.close()
        damage_table_pages(self.catalog_path)
        with mock.patch.object(server, "AUTO_RECOVER", True):
            self.assertIsNotNone(server.open_catalog())
        self.assertEqual(server.CATALOG_NOTICE["status"], "recovered")

    def test_a_newer_catalog_is_reported_incompatible_not_damaged(self):
        cat = self.seed(photos=1, history=0)
        with cat.write() as conn:
            conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        cat.close()
        self.assertIsNone(server.open_catalog())
        self.assertEqual(server.CATALOG_NOTICE["status"], "incompatible")
        self.assertFalse(server.recovery_status()["catalog"]["damaged"])

    def test_rebuild_salvages_quarantines_and_requests_a_restart(self):
        cat = self.seed()
        cat.close()
        damage_table_pages(self.catalog_path)
        server.open_catalog()
        result = server.recovery_action({"action": "rebuild"})
        self.assertTrue(result["ok"] and result["restart"])
        self.assertTrue((Path(result["quarantine"]) / "library.sqlite3").is_file())
        server.request_restart.assert_called_once()
        self.assertTrue(catalog_module.Catalog(self.catalog_path).quick_check_ok())

    def test_restore_of_a_chosen_backup_backs_up_the_live_catalog_first(self):
        cat = self.seed(photos=3, history=1)
        backups = self.catalog_path.parent / "Backups"
        archive = cat.backup(backups)
        cat.close()
        server.open_catalog()
        server.CATALOG.add_history(1, "after", {"grade": {"exposure": 1}})
        result = server.recovery_action({"action": "restore",
                                         "archive": str(archive)})
        self.assertTrue(result["restart"])
        self.assertEqual(len(list(backups.glob("LightTable-catalog-*.zip"))), 2,
                         "the edits about to be replaced were backed up")
        self.assertIsNone(server.CATALOG, "the live handle is retired")
        self.assertEqual(server.CATALOG_NOTICE["status"], "restarting")
        restored = catalog_module.Catalog(self.catalog_path)
        self.assertEqual(len(restored.history_for(1)), 1)
        restored.close()

    def test_restore_refuses_archives_outside_the_backup_folder(self):
        cat = self.seed(photos=1, history=0)
        cat.close()
        server.open_catalog()
        with self.assertRaises(ValueError):
            server.recovery_action({"action": "restore",
                                    "archive": str(self.root / "x.zip")})

    def test_reset_needs_confirmation_then_starts_empty(self):
        cat = self.seed(photos=2, history=0)
        cat.close()
        server.open_catalog()
        with self.assertRaises(ValueError):
            server.recovery_action({"action": "reset"})
        result = server.recovery_action({"action": "reset", "confirm": True})
        self.assertTrue(result["restart"])
        fresh = catalog_module.Catalog(self.catalog_path)
        self.assertEqual(fresh.stats()["images"], 0)
        fresh.close()

    def test_verify_and_repair_update_the_health_summary(self):
        cat = self.seed(photos=5, history=1)
        cat.close()
        raw = sqlite3.connect(self.catalog_path)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO iptc(image_id, title) VALUES(4242, 'orphan')")
        raw.commit()
        raw.close()
        server.open_catalog()
        verify = server.recovery_action({"action": "verify"})["verify"]
        self.assertFalse(verify["ok"])
        self.assertEqual(server.library_health_summary()["status"], "attention")
        self.assertFalse(server.health_payload()["library"]["lastVerifyOk"])
        repaired = server.recovery_action({"action": "repair"})
        self.assertEqual(repaired["removedOrphans"]["iptc"], 1)
        self.assertEqual(server.library_health_summary()["status"], "ok")

    def test_quarantined_photos_are_refused_and_can_be_released(self):
        cat = self.seed(photos=1, history=0)
        cat.close()
        server.open_catalog()
        server.recovery_action({"action": "set-aside", "name": "1:f0.jpg"})
        with self.assertRaises(server.APIError) as caught:
            server.guard_photo("1:f0.jpg")
        self.assertEqual(caught.exception.code, "quarantined")
        self.assertEqual(caught.exception.status, 423)
        with self.assertRaises(server.APIError):
            server.guard_photo("1:f0.jpg::lighttable-copy::v2")
        self.assertEqual(server.library_health_summary()["quarantined"], 1)
        released = server.recovery_action({"action": "release", "name": "1:f0.jpg"})
        self.assertTrue(released["released"])
        server.guard_photo("1:f0.jpg")

    def test_maintenance_checkpoints_backs_up_and_verifies(self):
        cat = self.seed(photos=3, history=2)
        cat.close()
        server.open_catalog()
        report = server.run_maintenance(server.CATALOG, force_verify=True)
        self.assertIn("backup", report)
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertTrue(report["checkpoint"]["ok"])
        self.assertTrue(report["verify"]["ok"])
        self.assertEqual(server.HEALTH_STATE["verify"], report["verify"])
        self.assertIn("disk", report)

    def test_the_process_lease_names_the_holder(self):
        cat = self.seed(photos=1, history=0)
        cat.close()
        instances = self.root / "instances"
        instances.mkdir()
        durable_io.atomic_write_json(instances / "9999.json", {
            "pid": os.getpid(), "port": 9999, "catalog": str(self.catalog_path),
            "host": "127.0.0.1", "headless": True,
        }, keep_backup=False)
        lock_path = self.catalog_path.with_suffix(".sqlite3.process-lock")
        import fcntl
        holder = lock_path.open("a+b")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.object(server, "CATALOG_PROCESS_LOCK", None), \
                    mock.patch.dict(os.environ,
                                    {"LIGHTTABLE_INSTANCE_DIR": str(instances)}):
                with self.assertRaises(server.CatalogLockedError) as caught:
                    server.acquire_catalog_process_lock()
            self.assertEqual(caught.exception.holder["port"], 9999)
            self.assertTrue(caught.exception.holder["headless"])
        finally:
            holder.close()

    def test_prefs_rewrite_preserves_unreadable_bytes(self):
        self.prefs.write_text("{broken")
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        with mock.patch.object(server.Handler, "_body", return_value={"a": 1}), \
                mock.patch.object(server.Handler, "_json") as sent, \
                mock.patch.object(server.Handler, "_enforce_security"), \
                mock.patch.object(server.Handler, "_log_request"):
            handler.path = "/api/prefs"
            handler.do_POST()
        sent.assert_called_with({"ok": True})
        self.assertEqual(json.loads(self.prefs.read_text()), {"a": 1})
        kept = list((self.root / "Recovery").glob("*/prefs.json"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
