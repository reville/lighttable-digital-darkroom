"""Check the populated Photos browser's real WebKit layout without activation."""
from pathlib import Path
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS WebKit required')
class ApplePhotosLayoutTests(unittest.TestCase):
    def test_populated_cards_contain_their_thumbnails_and_labels(self):
        index = (ROOT / 'web/index.html').read_text()
        styles = '\n'.join((ROOT / path.lstrip('/')).read_text()
                           for path in re.findall(r'<link rel="stylesheet" href="([^"]+)"', index))
        dialog = index[index.index('<div class="modal-backdrop apple-photos-backdrop"'):].split('</body>')[0]
        controller = (ROOT / 'web/apple-photos.js').read_text().split('\n', 1)[1].replace('export function ', 'function ')
        photo = base64.b64encode((ROOT / 'tests/fixtures/photos/field.jpg').read_bytes()).decode()
        script = r'''
const t = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);
const formatNumber = String;
CONTROLLER
let requestId;
const browser = installApplePhotosBrowser({el: id => document.getElementById(id),
  sendNative: (action, data) => { if (action === 'browseApplePhotos') requestId = data.requestId; },
  nativeBridge: () => true, onImported: async () => {}, onViewImported: async () => {}});
browser.nativeEvent({type: 'sources', photosBrowserAvailable: true});
browser.open();
const items = Array.from({length: 60}, (_, i) => ({id: String(i), name: `Photo ${i}.jpg`}));
browser.nativeEvent({type: 'applePhotosPage', requestId, items, offset: 0, total: 600, hasMore: true});
for (const item of items) browser.nativeEvent({type: 'applePhotosThumbnail', requestId, id: item.id, data: PHOTO});
Promise.all([...document.querySelectorAll('.apple-photos-preview img')].map(img => img.decode())).then(() => {
  const rect = node => { const r = node.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height, bottom:r.bottom}; };
  const cards = [...document.querySelectorAll('.apple-photos-card')];
  cards[0].click();
  const results = cards.map(card => ({card:rect(card), preview:rect(card.querySelector('.apple-photos-preview')), label:rect(card.querySelector('.apple-photos-name'))}));
  const grid = document.getElementById('applePhotosGrid');
  window.webkit.messageHandlers.result.postMessage({cards:results, scrollHeight:grid.scrollHeight, height:grid.clientHeight,
    selected:cards[0].getAttribute('aria-pressed'), importDisabled:document.getElementById('applePhotosImport').disabled});
}).catch(error => window.webkit.messageHandlers.result.postMessage({error:String(error)}));
'''.replace('CONTROLLER', controller).replace('PHOTO', json.dumps('data:image/jpeg;base64,' + photo))
        html = '<!doctype html><style>' + styles + '</style><button id="applePhotosOpen"></button><button id="importPhotosBtn"></button>' + dialog + '<script>' + script + '</script>'
        with tempfile.TemporaryDirectory(prefix='lighttable-photos-layout-') as directory:
            directory = Path(directory)
            (directory / 'page.html').write_text(html)
            (directory / 'main.swift').write_text(HARNESS)
            executable = directory / 'layout-test'
            build = subprocess.run(['swiftc', '-module-cache-path', str(directory / 'modules'),
                                    str(directory / 'main.swift'), '-o', str(executable)], capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            for width in (1100, 600):
                with self.subTest(width=width):
                    run = subprocess.run([str(executable), str(directory / 'page.html'), str(width)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    result = json.loads(run.stdout)
                    self.assertNotIn('error', result)
                    self.assertEqual(len(result['cards']), 60)
                    self.assertEqual(result['selected'], 'true')
                    self.assertFalse(result['importDisabled'])
                    self.assertGreater(result['scrollHeight'], result['height'])
                    for row in result['cards']:
                        self.assertGreater(row['preview']['height'], 100)
                        self.assertGreaterEqual(row['card']['bottom'] + 1, row['label']['bottom'])
                        self.assertGreaterEqual(row['label']['y'] + 1, row['preview']['bottom'])
                    first = result['cards'][0]['card']
                    next_row = next(row['card'] for row in result['cards'] if row['card']['y'] > first['y'] + 1)
                    self.assertGreaterEqual(next_row['y'], first['bottom'] + 10)


HARNESS = r'''
import AppKit
import WebKit
final class Result: NSObject, WKScriptMessageHandler {
    var value: Any?
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) { value = message.body }
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let width = Double(CommandLine.arguments[2])!
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 800), styleMask: [.titled], backing: .buffered, defer: false)
let result = Result()
let configuration = WKWebViewConfiguration()
configuration.userContentController.add(result, name: "result")
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: 800), configuration: configuration)
window.contentView = web
let url = URL(fileURLWithPath: CommandLine.arguments[1])
web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
let deadline = Date().addingTimeInterval(20)
while result.value == nil && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
guard let value = result.value else { print("Timed out waiting for Photos layout"); exit(1) }
let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
'''
