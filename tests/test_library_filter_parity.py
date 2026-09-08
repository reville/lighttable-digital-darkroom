"""Compare real folder/catalog payloads through the browser's filter pipeline."""
import json
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import catalog_scan
import server
from tests.test_server_catalog import CatalogServerTestCase, make_photo


class LibraryFilterParityTests(CatalogServerTestCase):
    def test_local_only_edits_match_after_actual_payload_and_browser_normalization(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is required to execute the browser filter pipeline")
        entries = {
            "mask.jpg": {"masks": [{"id": "mask", "type": "radial", "grade": {"exposure": 1}}]},
            "heal.jpg": {"heals": [{"id": "spot", "mode": "clone", "target": [0.5, 0.5]}]},
            "optics.jpg": {"optics": {"rotate": 2}},
            "neutral.jpg": {},
        }
        for name in entries:
            make_photo(self.root / name)
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        for name, entry in entries.items():
            self.catalog.save_state(self.catalog.image_id_for(self.source, name), entry)
        catalog_rows, _ = server.library_payload()
        with mock.patch.object(server, "catalog_handle", return_value=None):
            server.write_state({"images": entries})
            folder_rows, _ = server.library_payload()
        script = r'''
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {normalizeMasks, normalizeHeals, normalizeOptics} from './web/editor-panels.js';
import {matchesLibraryFilters} from './web/library-filters.js';
const payload = JSON.parse(readFileSync(0, 'utf8'));
const source = readFileSync('web/app.js', 'utf8');
const normalize = source.match(/^function normalizeLibraryImage\([^]*?^}/m)[0];
const results = {};
for (const [mode, rows] of Object.entries(payload)) {
  const context = {S: {catalogEnabled: mode === 'catalog'}, normalizeMasks, normalizeHeals, normalizeOptics};
  vm.runInNewContext(normalize, context);
  results[mode] = rows.map(row => {
    const image = context.normalizeLibraryImage(row);
    return {name: image.name.split(':').at(-1), edited: matchesLibraryFilters(image, [], 'edited'),
      unedited: matchesLibraryFilters(image, [], 'unedited')};
  });
}
console.log(JSON.stringify(results));
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script],
            input=json.dumps({"catalog": catalog_rows, "folder": folder_rows}),
            text=True, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads(result.stdout)
        for mode, rows in results.items():
            by_name = {row["name"]: row for row in rows}
            for name in entries:
                with self.subTest(mode=mode, name=name):
                    expected = name != "neutral.jpg"
                    self.assertEqual(by_name[name]["edited"], expected)
                    self.assertEqual(by_name[name]["unedited"], not expected)
