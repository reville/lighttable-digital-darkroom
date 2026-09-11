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
