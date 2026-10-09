# SPDX-License-Identifier: GPL-3.0-only
"""Run the production Store grant lifecycle with mocked OS authority operations.

Real files and production init/restore/persist exercise the destructive-offline
regression; only macOS bookmark/scoping/reachability calls are substituted.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires Swift on macOS')
class BookmarkBridgeTests(unittest.TestCase):
    def test_saved_authority_and_worker_transfer_lifecycle(self):
        source = (ROOT / 'app/main.swift').read_text()
        bridge = source.split('// MARK: - Store bookmark bridge', 1)[1].split('\n#endif', 1)[0]
        bridge = bridge.replace('private init()', 'init()')
        bridge = bridge.replace('URL(resolvingBookmarkData:', 'mockResolve(resolvingBookmarkData:')
        for method in ('checkResourceIsReachable', 'startAccessingSecurityScopedResource',
                       'stopAccessingSecurityScopedResource', 'bookmarkData'):
            bridge = bridge.replace(f'url.{method}(', f'mock{method}(url, ')
        bridge = bridge.replace('previous?.stopAccessingSecurityScopedResource()',
                                'if let previous { mockstopAccessingSecurityScopedResource(previous) }')
        bridge = bridge.replace('$0.stopAccessingSecurityScopedResource()',
                                'mockstopAccessingSecurityScopedResource($0)')
        # No-argument calls get no dangling comma after substitution.
        bridge = bridge.replace('(url, )', '(url)')
        harness = r'''
import Foundation
let support = URL(fileURLWithPath: CommandLine.arguments[1])
func lightTableSupportDirectory() -> URL { support }
func storeFolderIsAccessible(_ path: String, selectedRoots: [URL], supportDirectory: URL) -> Bool {
    selectedRoots.contains { $0.path == path }
}
var mounted = false
var revoked = false
var transferFailure = false
var starts = 0
var stops = 0
func mockResolve(resolvingBookmarkData data: Data, options: URL.BookmarkResolutionOptions,
                 relativeTo: URL?, bookmarkDataIsStale: inout Bool) throws -> URL {
    precondition(options.contains(.withoutMounting) && options.contains(.withoutUI))
    let value = String(data: data, encoding: .utf8)!
    guard mounted, value == "valid" else { throw CocoaError(.fileReadNoSuchFile) }
    bookmarkDataIsStale = true // authorized stale data must refresh safely
    return URL(fileURLWithPath: "/fixture/photos")
}
func mockstartAccessingSecurityScopedResource(_ url: URL) -> Bool {
    if revoked { return false }; starts += 1; return true
}
func mockstopAccessingSecurityScopedResource(_ url: URL) { stops += 1 }
func mockcheckResourceIsReachable(_ url: URL) throws -> Bool { mounted }
func mockbookmarkData(_ url: URL, options: URL.BookmarkCreationOptions,
                      includingResourceValuesForKeys: [URLResourceKey]?, relativeTo: URL?) throws -> Data {
    guard mounted, !transferFailure else { throw CocoaError(.fileReadNoPermission) }
    return Data((options.contains(.withSecurityScope) ? "valid" : "worker").utf8)
}
'''
        checks = r'''
func read(_ name: String) throws -> [String: String] {
    try JSONDecoder().decode([String: String].self, from: Data(contentsOf: support.appendingPathComponent(name)))
}
let saved = ["/fixture/photos": Data("valid".utf8).base64EncodedString(),
             "/revoked": Data("denied".utf8).base64EncodedString(), "/bad": "!!!"]
try JSONEncoder().encode(saved).write(to: support.appendingPathComponent("file-grants.json"))
private var bridge: StoreFileAccess? = StoreFileAccess()
assertRead((try read("worker-grants.json")).isEmpty)
assertRead((try read("file-grants.json")).count == 2)
precondition(!bridge!.contains("/fixture/photos"))
for _ in 0..<3 {
    mounted = true
    bridge!.restoreAvailableGrants()
    precondition(bridge!.contains("/fixture/photos"))
    precondition(!bridge!.contains("/revoked"))
    assertRead((try read("worker-grants.json")).count == 1)
    mounted = false
    bridge!.restoreAvailableGrants()
    assertRead((try read("worker-grants.json")).isEmpty)
    assertRead((try read("file-grants.json")).count == 2)
    bridge = nil
    bridge = StoreFileAccess() // offline startup must not erase saved authority
    assertRead((try read("worker-grants.json")).isEmpty)
}
mounted = true; revoked = true
bridge!.restoreAvailableGrants()
precondition(!bridge!.contains("/fixture/photos"))
assertRead((try read("worker-grants.json")).isEmpty)
revoked = false; transferFailure = true
bridge!.restoreAvailableGrants()
assertRead((try read("worker-grants.json")).isEmpty)
transferFailure = false
bridge!.restoreAvailableGrants()
assertRead((try read("worker-grants.json")).count == 1)
bridge = nil
precondition(starts == stops)
print("production bridge offline/remount/relaunch/revoked/transfer failure passed")
'''
        checks = 'func assertRead(_ value: Bool) { precondition(value) }\n' + checks
        with tempfile.TemporaryDirectory(prefix='lighttable-bookmark-bridge-') as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            main = root / 'main.swift'
            main.write_text(harness + bridge + '\n' + checks)
            env = dict(os.environ, SWIFT_MODULECACHE_PATH=str(root / 'swift-cache'),
                       CLANG_MODULE_CACHE_PATH=str(root / 'clang-cache'))
            compiled = subprocess.run(['swiftc', str(main), '-o', str(root / 'test')], env=env,
                                      capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            subprocess.run([str(root / 'test'), str(data)], check=True,
                           capture_output=True, text=True, timeout=10)
