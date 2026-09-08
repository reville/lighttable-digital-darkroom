// Compiled with the production NativePreview.swift by native_processing_preview.py.
// Read back submitted MTKView drawables: snapshot() would silently rerender and
// miss stale drawable, navigation, and compare restoration failures.
import AppKit
import MetalKit

@main
struct NativeProcessingPreview {
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() { fputs("FAIL: \(message)\n", stderr); exit(1) }
    }

    static func wait(_ description: String, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(10)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.005))
        }
        require(condition(), "timed out: \(description)")
    }

    static func main() throws {
        require(CommandLine.arguments.count == 4, "expected base URL, manifest, output directory")
        guard let device = MTLCreateSystemDefaultDevice() else {
            fputs("BLOCKED: a macOS graphical session with a Metal device is required\n", stderr)
            exit(1)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.finishLaunching()
        guard let renderer = NativePreviewRenderer(), let readQueue = device.makeCommandQueue() else {
            fputs("BLOCKED: production Metal renderer could not be created\n", stderr)
            exit(1)
        }
        let base = URL(string: CommandLine.arguments[1])!
        let manifest = try JSONSerialization.jsonObject(with: Data(contentsOf:
            URL(fileURLWithPath: CommandLine.arguments[2]))) as! [[String: Any]]
        let output = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 512, height: 384),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "LightTable processing correctness — production Metal renderer"
        window.contentView!.addSubview(renderer.view)
        // This flag permits test-only readback. Shader, uploads, load/cache,
        // automatic drawable sizing, and draw lifecycle are production code.
        renderer.view.framebufferOnly = false
        window.orderFrontRegardless()
        defer { window.orderOut(nil) }
        var completed = -1
        var presented = Set<Int>()
        renderer.onInteractionPresented = { sample in
            let frame = sample["frame"] as? Int ?? -1
            if sample["measurement"] as? String == "gpu-completed" { completed = frame }
            if sample["measurement"] as? String == "drawable-presented" { presented.insert(frame) }
        }
        var records = [[String: Any]]()
        var previousSize = CGSize.zero
        var originalLoaded = false

        for (index, item) in manifest.enumerated() {
            let name = item["name"] as! String
            let width = item["width"] as! Int, height = item["height"] as! Int
            let size = CGSize(width: width, height: height)
            if size != previousSize {
                let scale = window.backingScaleFactor
                renderer.setFrame(NSRect(x: 0, y: 0, width: Double(width) / scale,
                                         height: Double(height) / scale), visible: true)
                // Finish any resize-triggered display before identifying the
                // drawable which the next explicitly requested action submits.
                RunLoop.main.run(until: Date().addingTimeInterval(0.05))
                renderer.view.releaseDrawables()
                previousSize = size
            }
            if item["type"] as? String == "compare", !originalLoaded {
                renderer.recordInteraction(["frame": -2])
                renderer.loadOriginal(NativeSurfaceDescription(payload: ["url": "b.png"], baseURL: base)!, generation: 1)
                wait("original image upload") { completed == -2 }
                originalLoaded = true
            }

            if item["type"] as? String == "processed" {
                // Edit setters can schedule frames. Finish source loading
                // before selecting the drawable which proves these edits.
                renderer.beginNavigation(generation: index + 1)
                let loadFrame = -1000 - index
                renderer.recordInteraction(["frame": loadFrame])
                let surface = NativeSurfaceDescription(payload: item["surface"] as! [String: Any], baseURL: base)!
                var loaded = false
                var failure: String?
                renderer.load(surface, generation: index + 1, grade: [:]) { result in
                    DispatchQueue.main.async {
                        if case .failure(let error) = result { failure = error.localizedDescription }
                        loaded = true
                    }
                }
                wait("processed source upload") { loaded && completed == loadFrame }
                require(failure == nil, "source load failed: \(failure ?? "")")
                RunLoop.main.run(until: Date().addingTimeInterval(0.05))
                renderer.view.releaseDrawables()
            }
            guard let drawable = renderer.view.currentDrawable else {
                fputs("BLOCKED: MTKView drawable unavailable; a graphical session is required\n", stderr)
                exit(1)
            }
            renderer.recordInteraction(["frame": index])
            var cacheHit = false
            if item["type"] as? String == "load" {
                renderer.beginNavigation(generation: index + 1)
                renderer.recordInteraction(["frame": index])
                let surface = NativeSurfaceDescription(payload: item["surface"] as! [String: Any], baseURL: base)!
                var loadFinished = false
                var loadError: String?
                renderer.load(surface, generation: index + 1, grade: item["grade"] as? [String: Any] ?? [:]) { result in
                    DispatchQueue.main.async {
                        switch result {
                        case .success(let timings): cacheHit = timings.textureCacheHit
                        case .failure(let error): loadError = error.localizedDescription
                        }
                        loadFinished = true
                    }
                }
                wait("loading \(name)") { loadFinished }
                require(loadError == nil, "\(name): \(loadError ?? "")")
            } else if item["type"] as? String == "processed" {
                renderer.updateEdits(optics: item["optics"] as? [String: Any] ?? [:],
                                     heals: item["heals"] as? [[String: Any]] ?? [])
                renderer.updateMasks(item["maskPayload"] as! [String: Any])
                renderer.updateGrade(item["grade"] as? [String: Any] ?? [:])
            } else if item["type"] as? String == "compare" {
                renderer.updateComparePosition(item["position"] as! Double)
            } else {
                renderer.updateGrade(item["grade"] as? [String: Any] ?? [:])
            }
            wait("submitted drawable for \(name)") { completed == index }
            require(drawable.texture.width == width && drawable.texture.height == height,
                    "\(name): drawable \(drawable.texture.width)x\(drawable.texture.height), expected \(width)x\(height), view size \(renderer.view.drawableSize)")
            require(renderer.view.currentDrawable?.drawableID != drawable.drawableID,
                    "\(name): presented drawable was reused")

            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
            descriptor.storageMode = .shared
            let readback = device.makeTexture(descriptor: descriptor)!
            let command = readQueue.makeCommandBuffer()!
            let blit = command.makeBlitCommandEncoder()!
            blit.copy(from: drawable.texture, to: readback)
            blit.endEncoding()
            command.commit()
            command.waitUntilCompleted()
            require(command.status == .completed, "\(name): display readback failed")
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            readback.getBytes(&pixels, bytesPerRow: width * 4,
                              from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)
            try Data(pixels).write(to: output.appendingPathComponent(name + ".bgra"))
            records.append(["name": name, "width": width, "height": height,
                            "drawable_id": String(drawable.drawableID), "gpu_completed": true,
                            "texture_cache_hit": cacheHit, "frame": index])
        }
        // Presentation callbacks are observational: GPU-completed output is
        // proven even if an occluded layer never reports physical presentation.
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        for index in records.indices {
            records[index]["drawable_presented_callback"] = presented.contains(index)
        }
        let report: [String: Any] = ["device": device.name, "frames": records,
            "proof": "submitted MTKView drawable readback; not a full-app screenshot"]
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            .write(to: output.appendingPathComponent("drawables.json"))
        print("Captured \(records.count) production Metal drawables on \(device.name)")
    }
}
