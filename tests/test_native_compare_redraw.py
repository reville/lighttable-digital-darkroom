"""Exercise compare through the real MTKView drawable lifecycle on macOS."""

import functools
import http.server
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCE = ROOT / "app" / "NativePreview.swift"


@unittest.skipUnless(sys.platform == "darwin", "requires AppKit and Metal")
class NativeCompareRedrawTests(unittest.TestCase):
    def test_compare_retires_drawables_and_restores_pixels_in_both_directions(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for name, color in (("after", (0, 255, 0)), ("before", (255, 0, 0))):
                Image.new("RGB", (128, 64), color).save(temporary / f"{name}.png")

            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

            handler = functools.partial(QuietHandler, directory=str(temporary))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                bundle = temporary / "CompareRedraw.app" / "Contents"
                resources = bundle / "Resources"
                resources.mkdir(parents=True)
                executable = bundle / "MacOS" / "CompareRedraw"
                executable.parent.mkdir()
                (bundle / "Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleExecutable": "CompareRedraw",
                    "CFBundleIdentifier": "com.lighttable.test.compare-redraw",
                    "CFBundlePackageType": "APPL",
                }))
                shutil.copy(ROOT / "app" / "NativePreview.metal", resources)
                harness = temporary / "CompareRedraw.swift"
                harness.write_text(HARNESS)
                compile_result = subprocess.run([
                    swiftc, "-swift-version", "5", "-module-cache-path",
                    str(temporary / "module-cache"), str(NATIVE_SOURCE),
                    str(harness), "-o", str(executable),
                ], capture_output=True, text=True, timeout=90)
                self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
                result = subprocess.run([
                    str(executable), f"http://127.0.0.1:{server.server_port}/",
                ], capture_output=True, text=True, timeout=30)
                if result.returncode == 77:
                    self.skipTest(result.stderr.strip())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS: 9 compare frames", result.stdout)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


HARNESS = r'''
import AppKit
import MetalKit

@main
struct CompareRedraw {
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() {
            fputs("FAIL: \(message)\n", stderr)
            exit(1)
        }
    }

    static func wait(_ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(5)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        require(condition(), "timed out waiting for GPU completion")
    }

    static func main() {
        guard MTLCreateSystemDefaultDevice() != nil else {
            fputs("Metal device unavailable on this host\n", stderr)
            exit(77)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.finishLaunching()
        guard let renderer = NativePreviewRenderer() else {
            fputs("FAIL: Metal renderer unavailable\n", stderr)
            exit(1)
        }
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 512, height: 256),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "LightTable compare redraw regression"
        window.contentView!.addSubview(renderer.view)
        // Only the test reads display textures; production remains framebuffer-only.
        renderer.view.framebufferOnly = false
        renderer.setFrame(NSRect(x: 0, y: 0, width: 512, height: 256), visible: true)
        window.orderFrontRegardless()
        defer { window.orderOut(nil) }

        let base = URL(string: CommandLine.arguments[1])!
        func surface(_ name: String) -> NativeSurfaceDescription {
            NativeSurfaceDescription(payload: ["url": name + ".png"], baseURL: base)!
        }
        var loaded = false
        renderer.load(surface("after"), generation: 1, grade: [:]) { result in
            if case .success = result { loaded = true }
        }
        wait { loaded }
        renderer.loadOriginal(surface("before"), generation: 1)
        renderer.updateComparePosition(1)
        wait {
            guard let image = renderer.snapshot(), let data = image.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data: data),
                  let color = bitmap.colorAt(x: 0, y: 0)?.usingColorSpace(.sRGB)
            else { return false }
            return color.redComponent > 0.9
        }

        var completed = -1
        renderer.onInteractionPresented = { sample in
            if sample["measurement"] as? String == "gpu-completed" {
                completed = sample["frame"] as? Int ?? -1
            }
        }
        let positions = [0.1, 0.9, 0.25, 0.75, 0.5, 0.98, 0.02, 1.0, 0.0]
        let device = renderer.view.device!
        let queue = device.makeCommandQueue()!
        for (index, position) in positions.enumerated() {
            // Acquiring before submission identifies the exact drawable used.
            // The next read must acquire a new frame, even with unchanged size.
            let previous = renderer.view.currentDrawable!
            renderer.recordInteraction(["frame": index])
            renderer.updateComparePosition(position)
            wait { completed == index }
            let next = renderer.view.currentDrawable!.drawableID
            require(next != previous.drawableID, "compare frame \(index) reused a presented drawable")

            // Read the frame actually submitted for display, not an offscreen
            // rerender or a color-managed NSImage/TIFF conversion.
            let width = previous.texture.width, height = previous.texture.height
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
            descriptor.storageMode = .shared
            let readback = device.makeTexture(descriptor: descriptor)!
            let command = queue.makeCommandBuffer()!
            let blit = command.makeBlitCommandEncoder()!
            blit.copy(from: previous.texture, to: readback)
            blit.endEncoding()
            command.commit()
            command.waitUntilCompleted()
            require(command.status == .completed, "display readback failed")
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            readback.getBytes(&pixels, bytesPerRow: width * 4,
                              from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)
            for y in 0..<height {
                for x in 0..<width {
                    let offset = (y * width + x) * 4
                    let red = pixels[offset + 2], green = pixels[offset + 1]
                    let before = position > 0 && (Double(x) + 0.5) / Double(width) <= position
                    require(before ? red > 230 && green < 25 : green > 230 && red < 25,
                            "stale display pixels at frame \(index), pixel \(x),\(y): \(red),\(green)")
                }
            }
        }
        print("PASS: 9 compare frames retire drawables and restore before/after pixels")
    }
}
'''
