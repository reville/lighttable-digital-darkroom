# SPDX-License-Identifier: GPL-3.0-only
"""Execute the native retention policy across unavailable/revoked lifecycle states."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires Swift')
class BookmarkRestoreTests(unittest.TestCase):
    def test_offline_remount_repeated_relaunch_and_invalid_authority(self):
        source = (ROOT / 'app/main.swift').read_text()
        policy = source.split('// BEGIN STORE BOOKMARK RESTORATION POLICY', 1)[1].split('// END STORE BOOKMARK RESTORATION POLICY', 1)[0]
        with tempfile.TemporaryDirectory(prefix='lighttable-bookmark-policy-') as folder:
            root = Path(folder)
            main = root / 'main.swift'
            main.write_text('import Foundation\n' + policy + '''
var saved = ["/volume/photos": Data("valid-authority".utf8).base64EncodedString(),
             "/revoked": Data("revoked-authority".utf8).base64EncodedString(),
             "/corrupt": "!!!", "/empty": ""]
for mounted in [false, true, false, false, true] {
    // Serialization simulates independent launches, with no implicit path grant.
    saved = try JSONDecoder().decode([String: String].self,
        from: JSONEncoder().encode(saved))
    let result = storeRestoreBookmarks(saved) { path, data in
        guard mounted, String(data: data, encoding: .utf8) == "valid-authority"
            else { return nil }
        return URL(fileURLWithPath: path)
    }
    for invalid in result.invalid { saved.removeValue(forKey: invalid) }
    precondition(saved["/volume/photos"] != nil)
    precondition(saved["/revoked"] != nil) // opaque data is inert, not authority
    precondition(saved["/corrupt"] == nil && saved["/empty"] == nil)
    precondition(result.active["/revoked"] == nil)
    precondition((result.active["/volume/photos"] != nil) == mounted)
    precondition(result.active.count == (mounted ? 1 : 0))
}
print("offline/remount/repeated-launch/revoked/corrupt passed")
''')
            env = dict(os.environ, SWIFT_MODULECACHE_PATH=str(root / 'swift-cache'),
                       CLANG_MODULE_CACHE_PATH=str(root / 'clang-cache'))
            subprocess.run(['swiftc', str(main), '-o', str(root / 'test')], env=env,
                           check=True, capture_output=True, timeout=90)
            subprocess.run([str(root / 'test')], check=True, capture_output=True, timeout=10)
