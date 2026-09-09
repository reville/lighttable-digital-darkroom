"""Check the production native pinch bridge without displaying a window."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "requires AppKit and WebKit")
class NativePinchTests(unittest.TestCase):
    def test_magnification_reaches_javascript_without_scaling_webkit(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        source = (ROOT / "app/main.swift").read_text()
        chrome = source[source.index("private enum WindowChrome"):
                        source.index("// MARK: - Locations")]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            main = directory / "main.swift"
            executable = directory / "pinch-test"
            main.write_text(HARNESS.replace("CHROME_SOURCE", chrome))
            for command in (
                [swiftc, "-swift-version", "5", "-module-cache-path",
                 str(directory / "modules"), str(main), "-o", str(executable)],
                [str(executable)],
            ):
                result = subprocess.run(command, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS: native pinch delivery", result.stdout)
            print(result.stdout.strip())


HARNESS = r'''
import AppKit
import WebKit

CHROME_SOURCE

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { print("FAIL: \(message)"); exit(1) }
}
func wait(_ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(10)
    while !condition() && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    require(condition(), "timed out waiting for WebKit")
}
final class Messages: NSObject, WKScriptMessageHandler {
    var ready = false
    var pinches: [[String: Double]] = []
    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.body as? String == "ready" { ready = true }
        if let payload = message.body as? [String: Double] { pinches.append(payload) }
    }
}
// Synthetic values enter the production NSEvent -> DOM boundary. No global
// event injection, activation, or physical trackpad claim is involved.
final class MagnifyEvent: NSEvent {
    var owner: NSWindow?
    var location = NSPoint.zero
    var amount: CGFloat = 0
    override var type: NSEvent.EventType { .magnify }
    override var window: NSWindow? { owner }
    override var locationInWindow: NSPoint { location }
    override var magnification: CGFloat { amount }
}
let foreground = NSWorkspace.shared.frontmostApplication?.processIdentifier
let application = NSApplication.shared
application.setActivationPolicy(.prohibited)
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 800),
    styleMask: [.titled], backing: .buffered, defer: false)
let root = NSView(frame: NSRect(x: 0, y: 0, width: 1000, height: 800))
let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
let messages = Messages()
config.userContentController.add(messages, name: "pinch")
let web = LightTableWebView(frame: NSRect(x: 30, y: 40, width: 900, height: 700), configuration: config)
web.allowsMagnification = false
root.addSubview(web)
window.contentView = root
web.loadHTMLString("""
<html><body><script>
window.addEventListener('lighttable-magnify', e => window.webkit.messageHandlers.pinch.postMessage(e.detail));
window.webkit.messageHandlers.pinch.postMessage('ready');
</script></body></html>
""", baseURL: nil)
wait { messages.ready }
let event = MagnifyEvent()
event.owner = window
event.location = web.convert(NSPoint(x: 350, y: web.isFlipped ? 250 : 450), to: nil)
event.amount = 0.2
require(web.forwardEditorMagnification(event), "own-window pinch must be consumed")
wait { messages.pinches.count == 1 }
let payload = messages.pinches[0]
require(abs((payload["factor"] ?? 0) - 1.2) < 0.000001, "magnification is an incremental scale")
require(payload["x"] == 350 && payload["y"] == 250, "window coordinates must become DOM coordinates")
event.amount = -0.25
require(web.forwardEditorMagnification(event), "reverse pinch must be consumed")
wait { messages.pinches.count == 2 }
require(messages.pinches[1]["factor"] == 0.75, "pinch-in must shrink the photo")
require(web.magnification == 1 && web.pageZoom == 1, "WebKit UI scale must stay unchanged")

event.owner = nil
require(!web.forwardEditorMagnification(event), "other windows must keep their events")
event.owner = window
event.location = NSPoint(x: -100, y: -100)
require(!web.forwardEditorMagnification(event), "outside points must keep their events")
event.location = web.convert(NSPoint(x: 350, y: 250), to: nil)
web.isHidden = true
require(!web.forwardEditorMagnification(event), "hidden views must not receive pinch")
web.isHidden = false
event.amount = .nan
require(web.forwardEditorMagnification(event), "invalid pinch is consumed without emitting JSON")
web.removeFromSuperview()
require(!web.forwardEditorMagnification(event), "detached views must not receive pinch")
require(!window.isVisible && !application.isActive, "test must never show or activate the window")
require(NSWorkspace.shared.frontmostApplication?.processIdentifier == foreground, "foreground app must be unchanged")
print("PASS: native pinch delivery, coordinates, direction, window scope, unchanged UI scale; no activation")
'''
