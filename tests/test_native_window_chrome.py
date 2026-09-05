"""Exercise the production chrome class against AppKit/WebKit hit testing."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "AppKit chrome is macOS-only")
class NativeWindowChromeTests(unittest.TestCase):
    def test_native_event_routing_and_drag_handoff(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        source = (ROOT / "app" / "main.swift").read_text()
        chrome = source[source.index("private enum WindowChrome"):
                        source.index("// MARK: - Locations")]
        harness = r'''
import AppKit
import WebKit

CHROME_SOURCE

final class DragRecordingWindow: NSWindow {
    var dragCount = 0
    override func performDrag(with event: NSEvent) { dragCount += 1 }
}
func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        print("FAIL: \(message)")
        exit(1)
    }
}
let application = NSApplication.shared
application.setActivationPolicy(.prohibited)
let window = DragRecordingWindow(
    contentRect: NSRect(x: 100, y: 100, width: 1200, height: 800),
    styleMask: [.titled, .resizable, .fullSizeContentView],
    backing: .buffered, defer: false)
let root = NSView(frame: NSRect(x: 0, y: 0, width: 1200, height: 800))
let configuration = WKWebViewConfiguration()
configuration.websiteDataStore = .nonPersistent()
// Nonzero frame origin checks the superview-to-web-view conversion too.
let web = LightTableWebView(
    frame: NSRect(x: 30, y: 40, width: 1100, height: 700),
    configuration: configuration)
root.addSubview(web)
window.contentView = root
// Supply a content descendant even without a visible page/WebContent process.
// This reproduces the event interception by WebKit's private content views.
let content = NSView(frame: web.bounds)
content.autoresizingMask = [.width, .height]
web.addSubview(content)

func layout(blocked: Bool = false, controls: [[String: Int]]? = nil) {
    web.updateWindowChromeLayout([
        "topBar": ["x": 0, "y": 0, "width": Int(web.bounds.width), "height": 48],
        "controls": controls ?? [
            ["x": 80, "y": 8, "width": 140, "height": 32],
            ["x": 300, "y": 8, "width": 450, "height": 32]],
        "blocked": blocked])
}
func localPoint(_ x: CGFloat, _ y: CGFloat) -> NSPoint {
    NSPoint(x: web.bounds.minX + x,
            y: web.isFlipped ? web.bounds.minY + y : web.bounds.maxY - y)
}
func target(_ x: CGFloat, _ y: CGFloat) -> NSView? {
    root.hitTest(web.convert(localPoint(x, y), to: root))
}
layout()
for x: CGFloat in [250, 800, 1000] {
    require(target(x, 24) === web, "top-bar background must reach the drag handler")
}
for x: CGFloat in [100, 200, 299, 500, 751] {
    require(target(x, 24) !== web, "controls and safety insets must stay in WebKit at \(x): \(String(describing: target(x, 24)))")
}
require(target(250, 680) !== web, "bottom content must never become the title bar")
require(target(250, 100) !== web, "photo workspace must stay in WebKit")
require(target(1150, 24) !== web, "outside points must not be claimed")

// Deliver the event to the actual native hit-test result. Record the OS drag
// handoff without entering Window Server's interactive drag loop in the test.
let event = NSEvent.mouseEvent(
    with: .leftMouseDown, location: web.convert(localPoint(250, 24), to: nil),
    modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber,
    context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
target(250, 24)?.mouseDown(with: event)
require(window.dragCount == 1, "background mouse-down must call NSWindow.performDrag")
layout(blocked: true)
require(target(250, 24) !== web, "modal blocks native chrome hit testing")
web.mouseDown(with: event)
require(window.dragCount == 1, "modal blocks drag handoff too")
layout(controls: [])
require(target(500, 24) === web, "empty controls must replace stale exclusions")
layout(blocked: true, controls: [])
require(target(500, 24) !== web, "empty controls must still update modal blocking")
web.resetWindowChromeLayout()
require(target(150, 24) !== web, "startup must not steal the Add Photos button")
require(target(250, 44) === web, "safe bottom strip works before layout arrives")
web.setFrameSize(NSSize(width: 900, height: 550))
layout()
require(target(800, 24) === web, "resized top bar stays draggable")
require(target(800, 530) !== web, "resized bottom content stays interactive")
print("PASS: native hit testing, drag handoff, controls, modals, reset, and resize")
'''.replace("CHROME_SOURCE", chrome)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            main = directory / "main.swift"
            executable = directory / "chrome-test"
            main.write_text(harness)
            for command in (
                [swiftc, "-swift-version", "5", "-module-cache-path",
                 str(directory / "modules"), str(main), "-o", str(executable)],
                [str(executable)],
            ):
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
