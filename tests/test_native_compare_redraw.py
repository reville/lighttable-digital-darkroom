"""Exercise compare and zoom through real Metal drawables without taking focus."""

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
        self.run_harness(HARNESS, "PASS: 9 compare frames")

    def test_zoom_submits_matching_geometry_and_pixels_before_returning(self):
        self.run_harness(ZOOM_HARNESS, "PASS: 8 atomic zoom frames", probe=True)

    def run_harness(self, harness_source, expected_output, *, probe=False):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for name, color in (("after", (0, 255, 0)), ("before", (255, 0, 0))):
                Image.new("RGB", (128, 64), color).save(temporary / f"{name}.png")

            # Coordinates encoded in R/G reveal stale UV mappings, stretching,
            # and incorrect clipping in the frame that was actually submitted.
            coordinates = Image.new("RGB", (256, 256))
            coordinates.putdata([(x, y, 0) for y in range(256) for x in range(256)])
            coordinates.save(temporary / "coordinates.png")
            coordinates.putdata([(x, y, 160) for y in range(256) for x in range(256)])
            coordinates.save(temporary / "coordinates-blue.png")

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
                harness.write_text(harness_source + BACKGROUND_FOCUS_GUARD)
                native_source = NATIVE_SOURCE
                if probe:
                    # Compile an instrumented copy; no test hooks enter the app.
                    source = NATIVE_SOURCE.read_text()
                    needle = "        drawable.present()"
                    self.assertEqual(source.count(needle), 1)
                    source = source.replace(needle, needle + "\n" + SUBMISSION_PROBE)
                    native_source = temporary / "NativePreview.swift"
                    native_source.write_text(source)
                compile_result = subprocess.run([
                    swiftc, "-swift-version", "5", "-module-cache-path",
                    str(temporary / "module-cache"), str(native_source),
                    str(harness), "-o", str(executable),
                ], capture_output=True, text=True, timeout=90)
                self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
                result = subprocess.run([
                    str(executable), f"http://127.0.0.1:{server.server_port}/",
                ], capture_output=True, text=True, timeout=30)
                if result.returncode == 77:
                    self.skipTest(result.stderr.strip())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected_output, result.stdout)
                self.assertIn("PASS: foreground PID unchanged; no app activation or Space change", result.stdout)
                print(result.stdout.strip())
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
        let focus = BackgroundFocusGuard()
        defer { focus.verify() }
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
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        window.orderBack(nil)
        focus.verify()
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


# Observe both transitions and final state, so briefly stealing focus or changing
# Space cannot be hidden by restoring the previous state before the test ends.
BACKGROUND_FOCUS_GUARD = r'''
final class BackgroundFocusGuard {
    let initialPID: pid_t
    var changedSpace = false
    var activated = false
    var observers: [NSObjectProtocol] = []

    init() {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
            fputs("Cannot verify foreground application on this host\n", stderr)
            exit(77)
        }
        initialPID = pid
        let center = NSWorkspace.shared.notificationCenter
        observers.append(center.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.changedSpace = true })
        observers.append(center.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey]
                as? NSRunningApplication else { return }
            if app.processIdentifier == ProcessInfo.processInfo.processIdentifier {
                self?.activated = true
            }
        })
    }

    func verify() {
        if changedSpace || activated ||
            NSWorkspace.shared.frontmostApplication?.processIdentifier != initialPID {
            fputs("FAIL: background renderer changed foreground application or Space\n", stderr)
            exit(1)
        }
        print("PASS: foreground PID unchanged; no app activation or Space change")
    }

    deinit {
        for observer in observers { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
    }
}
'''


SUBMISSION_PROBE = '''        NativeZoomSubmission.record(
            drawable: drawable, command: commandBuffer, view: view, viewport: viewport)
'''


ZOOM_HARNESS = r'''
import AppKit
import MetalKit

// This observer is inserted only into the test's temporary Swift source copy.
// It retains the exact production drawable after presentation is submitted,
// including its mapping and transaction state, without creating another render.
struct NativeZoomSubmission {
    static var latest: NativeZoomSubmission?
    static var count = 0
    let drawable: CAMetalDrawable
    let command: MTLCommandBuffer
    let frame: NSRect
    let viewport: SIMD4<Float>
    let actionsDisabled: Bool
    let transactional: Bool

    static func record(drawable: CAMetalDrawable, command: MTLCommandBuffer,
                       view: MTKView, viewport: SIMD4<Float>) {
        count += 1
        latest = NativeZoomSubmission(
            drawable: drawable, command: command, frame: view.frame, viewport: viewport,
            actionsDisabled: CATransaction.disableActions(),
            transactional: view.presentsWithTransaction)
    }
}

@main
struct ZoomRedraw {
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
        let focus = BackgroundFocusGuard()
        defer { focus.verify() }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.finishLaunching()
        guard let renderer = NativePreviewRenderer() else {
            fputs("FAIL: Metal renderer unavailable\n", stderr)
            exit(1)
        }
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 640, height: 480),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "LightTable zoom redraw regression"
        window.contentView!.addSubview(renderer.view)
        renderer.view.framebufferOnly = false
        renderer.setFrame(NSRect(x: 0, y: 0, width: 256, height: 256), visible: true)
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        window.orderBack(nil)
        focus.verify()
        defer { window.orderOut(nil) }

        let base = URL(string: CommandLine.arguments[1])!
        let surface = NativeSurfaceDescription(payload: ["url": "coordinates.png"], baseURL: base)!
        var loaded = false
        renderer.load(surface, generation: 1, grade: [:]) { result in
            if case .success = result { loaded = true }
        }
        wait { loaded }
        var completed = -1
        var presentationCallbacks = 0
        renderer.onInteractionPresented = { sample in
            if sample["measurement"] as? String == "gpu-completed" {
                completed = sample["frame"] as? Int ?? -1
            } else if sample["measurement"] as? String == "drawable-presented" {
                presentationCallbacks += 1
            }
        }
        let queue = renderer.view.device!.makeCommandQueue()!
        func checkSubmission(_ submitted: NativeZoomSubmission, frame: NSRect,
                             scale: SIMD2<Float>, offset: SIMD2<Float>,
                             exposure: Float = 0, blue: Float = 0, label: String) {
            // Reference the documented exposure operation in linear light.
            func exposed(_ byte: Float) -> Float {
                let srgb = byte / 255
                let linear = srgb <= 0.04045 ? srgb / 12.92 : pow((srgb + 0.055) / 1.055, 2.4)
                let scaled = linear * pow(2, exposure)
                return min(255, max(0, 255 * (scaled <= 0.0031308
                    ? scaled * 12.92 : 1.055 * pow(scaled, 1 / 2.4) - 0.055)))
            }
            require(submitted.actionsDisabled && submitted.transactional,
                    "\(label) did not submit geometry and drawable in one transaction")
            require(submitted.frame == frame &&
                    submitted.viewport == SIMD4(scale.x, scale.y, offset.x, offset.y),
                    "\(label) submitted stale frame or UV mapping")
            let width = submitted.drawable.texture.width
            let height = submitted.drawable.texture.height
            let expectedSize = renderer.view.convertToBacking(renderer.view.bounds).size
            require(width == Int(expectedSize.width) && height == Int(expectedSize.height),
                    "\(label) used old drawable dimensions \(width)x\(height)")
            submitted.command.waitUntilCompleted()
            require(submitted.command.status == .completed, "zoom GPU work failed")

            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
            descriptor.storageMode = .shared
            let readback = renderer.view.device!.makeTexture(descriptor: descriptor)!
            let command = queue.makeCommandBuffer()!
            let blit = command.makeBlitCommandEncoder()!
            blit.copy(from: submitted.drawable.texture, to: readback)
            blit.endEncoding()
            command.commit()
            command.waitUntilCompleted()
            require(command.status == .completed, "zoom display readback failed")
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            readback.getBytes(&pixels, bytesPerRow: width * 4,
                              from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)
            for y in stride(from: 8, to: height - 8, by: 17) {
                for x in stride(from: 8, to: width - 8, by: 17) {
                    let sourceX = (Float(x) + 0.5) / Float(width) * scale.x + offset.x
                    let sourceY = (Float(y) + 0.5) / Float(height) * scale.y + offset.y
                    let expectedRed = exposed(min(255, max(0, sourceX * 256 - 0.5)))
                    let expectedGreen = exposed(min(255, max(0, sourceY * 256 - 0.5)))
                    let at = (y * width + x) * 4
                    require(abs(Float(pixels[at + 2]) - expectedRed) <= 2 &&
                            abs(Float(pixels[at + 1]) - expectedGreen) <= 2 &&
                            abs(Float(pixels[at]) - exposed(blue)) <= 2,
                            "stretched/stale \(label) pixel \(x),\(y): " +
                            "\(pixels[at + 2]),\(pixels[at + 1]); expected \(expectedRed),\(expectedGreen)")
                }
            }
        }
        // Square source: each pair describes an isotropic zoom of the source,
        // clipped into changing viewport aspect ratios, including pan-only and
        // returning to Fit. Different X/Y mappings expose stretched old frames.
        let transitions: [(NSRect, SIMD2<Float>, SIMD2<Float>)] = [
            (NSRect(x: 0, y: 0, width: 384, height: 320), SIMD2(0.75, 0.625), SIMD2(0.125, 0.1875)),
            (NSRect(x: 0, y: 0, width: 512, height: 320), SIMD2(0.5, 0.3125), SIMD2(0.25, 0.34375)),
            (NSRect(x: 0, y: 0, width: 512, height: 320), SIMD2(0.5, 0.3125), SIMD2(0.375, 0.125)),
            (NSRect(x: 0, y: 0, width: 320, height: 384), SIMD2(0.3125, 0.375), SIMD2(0.34375, 0.3125)),
            (NSRect(x: 7, y: 9, width: 256, height: 384), SIMD2(0.25, 0.375), SIMD2(0.625, 0.1875)),
            (NSRect(x: 0, y: 0, width: 512, height: 256), SIMD2(0.5, 0.25), SIMD2(0.25, 0.375)),
            (NSRect(x: 0, y: 0, width: 320, height: 320), SIMD2(1, 1), SIMD2(0, 0)),
            (NSRect(x: 0, y: 0, width: 256, height: 256), SIMD2(1, 1), SIMD2(0, 0)),
        ]
        for (index, transition) in transitions.enumerated() {
            let (frame, scale, offset) = transition
            NativeZoomSubmission.latest = nil
            let previousCount = NativeZoomSubmission.count
            renderer.recordInteraction(["frame": index])
            renderer.setFrame(frame, uvScale: scale, uvOffset: offset, visible: true)
            // Check before pumping the run loop. A deferred redraw recreates
            // the original bug even if its eventual screenshot looks correct.
            require(NativeZoomSubmission.count == previousCount + 1,
                    "zoom \(index) returned before submitting its matching frame")
            guard let submitted = NativeZoomSubmission.latest else {
                require(false, "missing submitted zoom frame"); return
            }
            checkSubmission(submitted, frame: frame, scale: scale, offset: offset,
                            label: "zoom \(index)")
            wait { completed == index }
            focus.verify()
            let unchangedCount = NativeZoomSubmission.count
            renderer.setFrame(frame, uvScale: scale, uvOffset: offset, visible: true)
            require(NativeZoomSubmission.count == unchangedCount, "unchanged zoom needlessly redrew")
        }
        // Changing viewport inside prepare must never display the old source
        // or old exposure with the new geometry, even for a cache hit.
        let replacements: [(String, Float, Float, Bool, NSRect, SIMD2<Float>, SIMD2<Float>)] = [
            ("coordinates-blue.png", -1, 160, false,
             NSRect(x: 0, y: 0, width: 512, height: 256), SIMD2(0.5, 0.25), SIMD2(0.25, 0.375)),
            ("coordinates.png", 0, 0, true,
             NSRect(x: 0, y: 0, width: 320, height: 384), SIMD2(0.3125, 0.375), SIMD2(0.625, 0.3125)),
        ]
        for (index, replacement) in replacements.enumerated() {
            let (name, exposure, blue, cached, frame, scale, offset) = replacement
            let label = cached ? "cached surface swap" : "HTTP surface swap"
            let surface = NativeSurfaceDescription(payload: ["url": name], baseURL: base)!
            NativeZoomSubmission.latest = nil
            let previousCount = NativeZoomSubmission.count
            var prepared = false
            var swapResult: Result<NativePreviewTimings, Error>?
            renderer.recordInteraction(["frame": index + 100])
            renderer.load(surface, generation: index + 2, grade: ["exposure": exposure], prepare: {
                renderer.setFrame(frame, uvScale: scale, uvOffset: offset, visible: true)
                require(NativeZoomSubmission.count == previousCount,
                        "\(label) submitted old pixels during preparation")
                prepared = true
            }) { result in
                DispatchQueue.main.async { swapResult = result }
            }
            if cached {
                require(NativeZoomSubmission.count == previousCount + 1,
                        "cached surface swap returned without its complete frame")
            }
            wait { swapResult != nil }
            guard case .success(let timings) = swapResult else {
                require(false, "\(label) failed to load: \(String(describing: swapResult))"); return
            }
            require(prepared && timings.textureCacheHit == cached,
                    "\(label) did not exercise the expected load path")
            require(NativeZoomSubmission.count == previousCount + 1,
                    "\(label) submitted more than one frame")
            guard let submitted = NativeZoomSubmission.latest else {
                require(false, "\(label) missing submitted frame"); return
            }
            checkSubmission(submitted, frame: frame, scale: scale, offset: offset,
                            exposure: exposure, blue: blue, label: label)
            focus.verify()
        }
        NativeZoomSubmission.latest = nil
        print("PASS: 8 atomic zoom frames preserve submitted geometry and clipped source pixels")
        print("PASS: HTTP and cached surface swaps submit exactly one matching texture/grade/viewport")
        print("Drawable presentation callbacks: \(presentationCallbacks); background window, no visible-screen claim")
    }
}
'''
