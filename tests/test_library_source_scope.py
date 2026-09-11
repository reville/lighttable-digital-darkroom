"""The window shows one source; the browser's scope filter has to agree."""
import json
from pathlib import Path
import shutil
import subprocess

import catalog_scan
import server
from tests.test_server_catalog import CatalogServerTestCase, make_photo

SCRIPT = r'''
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const {rows, states} = JSON.parse(readFileSync(0, 'utf8'));
const source = readFileSync('web/app.js', 'utf8');
const scope = source.match(/^function inFolderScope\([^]*?^}/m)[0];
const results = {};
for (const [label, S] of Object.entries(states)) {
  const context = {S};
  vm.runInNewContext(scope, context);
  results[label] = rows.filter(context.inFolderScope).map(row => row.name);
}
console.log(JSON.stringify(results));
'''

SCOPE_FRAGMENT_SCRIPT = r'''
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const {states} = JSON.parse(readFileSync(0, 'utf8'));
const source = readFileSync('web/app.js', 'utf8');
const spec = source.match(/^function sourceScopeSpec\([^]*?^}/m)[0];
const folder = source.match(/^function activeFolderId\([^]*?^}/m)[0];
const results = {};
for (const [label, S] of Object.entries(states)) {
  const context = {S};
  vm.runInNewContext(spec + '\n' + folder, context);
  results[label] = {
    spec: context.sourceScopeSpec(),
    folder: context.activeFolderId(),
  };
}
console.log(JSON.stringify(results));
'''

PENDING_COLLECTION_SCRIPT = r'''
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const {states} = JSON.parse(readFileSync(0, 'utf8'));
const source = readFileSync('web/app.js', 'utf8');
const pending = source.match(/^function applyPendingActiveCollection\([^]*?^}/m)[0];
const results = {};
for (const [label, S] of Object.entries(states)) {
  const context = {S};
  vm.runInNewContext(pending, context);
  context.applyPendingActiveCollection();
  results[label] = {active: S.activeCollection, pending: S.pendingActiveCollection};
}
console.log(JSON.stringify(results));
'''


class LibrarySourceScopeTests(CatalogServerTestCase):
    def test_browser_scope_drops_rows_from_the_other_sources(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is required to execute the browser scope filter")
        elsewhere = Path(self._dir.name) / "elsewhere"
        make_photo(elsewhere / "c.jpg", (200, 60, 40))
        # The same relative folder name in a second source is the case that
        # leaked: folder paths are only unique within their own source.
        make_photo(elsewhere / "sub" / "d.jpg", (40, 200, 60))
        second = self.catalog.add_source(elsewhere)
        catalog_scan.scan_source(self.catalog, second, read_metadata_for_new=False)

        rows = server.browser_catalog_query({"limit": 50})["items"]
        self.assertEqual({row["sourceId"] for row in rows}, {self.source, second})

        states = {
            "root": {"primarySourceId": self.source, "activeFolder": "",
                     "includeSubfolders": True},
            "subfolder": {"primarySourceId": self.source, "activeFolder": "sub",
                          "includeSubfolders": False},
            "folder mode": {"primarySourceId": None, "activeFolder": "",
                            "includeSubfolders": True},
        }
        result = subprocess.run(
            ["node", "--input-type=module", "-e", SCRIPT],
            input=json.dumps({"rows": rows, "states": states}),
            text=True, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = json.loads(result.stdout)

        self.assertEqual(sorted(kept["root"]),
                         sorted([self.qualified("a.jpg"),
                                 self.qualified("sub/b.jpg")]))
        self.assertEqual(kept["subfolder"], [self.qualified("sub/b.jpg")])
        # Folder mode has no catalog sources, so nothing may be filtered out.
        self.assertEqual(len(kept["folder mode"]), len(rows))

    def test_scope_queries_fail_closed_without_their_id(self):
        # A scoped query that lost its identifier must match nothing, not every
        # source or folder the catalog knows about.
        self.assertEqual(
            server.browser_catalog_query({"scope": "source"})["total"], 0)
        self.assertEqual(
            server.browser_catalog_query({"scope": "folder"})["total"], 0)
        self.assertEqual(
            server.browser_catalog_query({
                "scope": "folder", "folderId": 0})["total"], 0)

    def test_source_filter_narrows_collection_queries(self):
        elsewhere = Path(self._dir.name) / "elsewhere"
        make_photo(elsewhere / "c.jpg", (200, 60, 40))
        second = self.catalog.add_source(elsewhere)
        catalog_scan.scan_source(self.catalog, second,
                                 read_metadata_for_new=False)
        image_id = self.catalog.image_id_for(self.source, "sub/b.jpg")
        copied_id = self.catalog.image_id_for(second, "c.jpg")
        collection = self.catalog.add_collection("Shared")
        self.catalog.set_collection_members(collection,
                                            [image_id, copied_id])

        page = server.browser_catalog_query({
            "scope": "collection", "collectionId": collection,
            "sourceId": self.source, "limit": 50})

        self.assertEqual([item["sourceId"] for item in page["items"]],
                         [self.source])
        self.assertEqual(page["total"], 1)

    def test_boot_payload_carries_the_folder_ids_queries_need(self):
        rows, snapshot = server.library_payload(limit=50)
        self.assertEqual(snapshot["folderIds"][""],
                         next(row["id"] for row in
                              server.catalog_folder_rows(self.source)
                              if row["relpath"] == ""))
        page = server.browser_catalog_query({
            "scope": "folder", "folderId": snapshot["folderIds"]["sub"],
            "includeSubfolders": True, "limit": 50})
        self.assertEqual([item["relpath"] for item in page["items"]],
                         ["sub/b.jpg"])
        self.assertEqual(page["total"], 1)

    def test_folder_counts_include_virtual_copies(self):
        image_id = self.catalog.image_id_for(self.source, "a.jpg")
        self.catalog.add_virtual_copy(image_id, "copy-1", "Alternate")

        rows, snapshot = server.library_payload(limit=50)
        root = next(row for row in snapshot["folders"] if row["path"] == "")
        by_path = {row["relpath"]: row["count"]
                   for row in server.catalog_folder_rows(self.source)}

        self.assertEqual(by_path[""], 2)
        self.assertEqual(root["totalCount"], snapshot["total"])
        self.assertEqual(snapshot["total"], 3)

    def test_browser_sends_the_folder_id_not_the_folder_path(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is required to execute the browser helpers")
        result = subprocess.run(
            ["node", "--input-type=module", "-e", SCOPE_FRAGMENT_SCRIPT],
            input=json.dumps({"states": {
                "catalog": {"catalogEnabled": True, "primarySourceId": 7,
                            "folderIds": {"sub": 12}, "activeFolder": "sub"},
                "folder mode": {"catalogEnabled": False, "primarySourceId": None,
                                "folderIds": {}, "activeFolder": "sub"},
                "missing id": {"catalogEnabled": True, "primarySourceId": 7,
                               "folderIds": {}, "activeFolder": "sub"},
            }}),
            text=True, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(result.stdout)

        self.assertEqual(sent["catalog"]["spec"], {"sourceId": 7})
        self.assertEqual(sent["catalog"]["folder"], 12)
        self.assertEqual(sent["folder mode"]["spec"], {})
        self.assertIsNone(sent["folder mode"]["folder"])
        self.assertIsNone(sent["missing id"]["folder"])

    def test_saved_collection_survives_prefs_arriving_first(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is required to execute the browser helpers")
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PENDING_COLLECTION_SCRIPT],
            input=json.dumps({"states": {
                "resolves saved": {
                    "pendingActiveCollection": "c1",
                    "library": {"collections": [{"id": "c1"}]},
                    "activeCollection": ""},
                "drops unknown": {
                    "pendingActiveCollection": "gone",
                    "library": {"collections": [{"id": "c1"}]},
                    "activeCollection": ""},
                "nothing pending": {
                    "pendingActiveCollection": None,
                    "library": {"collections": []},
                    "activeCollection": "keep"},
            }}),
            text=True, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)

        self.assertEqual(applied["resolves saved"]["active"], "c1")
        self.assertIsNone(applied["resolves saved"]["pending"])
        self.assertEqual(applied["drops unknown"]["active"], "")
        self.assertEqual(applied["nothing pending"]["active"], "keep")
