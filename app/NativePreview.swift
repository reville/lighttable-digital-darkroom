import AppKit
import MetalKit
import Darwin

// Darwin imports shm_open as unavailable because its mode argument is variadic.
// Opening an existing server-owned segment needs only the two fixed arguments.
@_silgen_name("shm_open")
private func openNativeSharedMemory(_ name: UnsafePointer<CChar>, _ flags: Int32) -> Int32

struct NativeSurfaceDescription {
    let url: URL
    let format: String
    let width: Int
    let height: Int
    let rowBytes: Int
    let headerBytes: Int
    let sharedMemory: SharedNativeSurface?
    let imageRegion: SIMD4<Float>

    init?(payload: [String: Any], baseURL: URL) {
        guard let rawURL = payload["url"] as? String,
              let url = URL(string: rawURL, relativeTo: baseURL)?.absoluteURL
        else { return nil }
        self.url = url
        if let region = payload["viewport"] as? [String: Any] {
            guard let x = region["x"] as? Int, let y = region["y"] as? Int,
                  let w = region["width"] as? Int, let h = region["height"] as? Int,
                  let fullW = region["fullWidth"] as? Int, let fullH = region["fullHeight"] as? Int,
                  x >= 0, y >= 0, w > 0, h > 0, fullW > 0, fullH > 0,
                  x <= fullW - w, y <= fullH - h else { return nil }
            imageRegion = SIMD4<Float>(Float(w) / Float(fullW), Float(h) / Float(fullH),
                Float(x) / Float(fullW), Float(y) / Float(fullH))
        } else {
            imageRegion = SIMD4<Float>(1, 1, 0, 0)
        }
        format = payload["format"] as? String ?? "image"
        width = payload["width"] as? Int ?? 0
        height = payload["height"] as? Int ?? 0
        rowBytes = payload["rowBytes"] as? Int ?? 0
        headerBytes = payload["headerBytes"] as? Int ?? 0
        sharedMemory = ["localhost", "127.0.0.1", "::1"].contains(url.host ?? "")
            ? (payload["sharedMemory"] as? [String: Any]).flatMap(SharedNativeSurface.init)
            : nil
    }
}

/// Immutable, per-render POSIX shared memory. The server owns the name and
/// unlinks it on cache eviction; an already mapped texture remains valid.
struct SharedNativeSurface {
    let name: String
    let length: Int
    let rowBytes: Int

    init?(payload: [String: Any]) {
        guard let name = payload["name"] as? String,
              name.hasPrefix("/lt-"), name.utf8.count < 32,
              !name.dropFirst().contains("/"),
              let length = payload["length"] as? Int,
              let rowBytes = payload["rowBytes"] as? Int,
              (payload["offset"] as? Int ?? 0) == 0,
              length > 0, length <= 256 * 1024 * 1024,
              rowBytes > 0, rowBytes % 256 == 0,
              length % Int(getpagesize()) == 0 else { return nil }
        self.name = name
        self.length = length
        self.rowBytes = rowBytes
    }

    func texture(device: MTLDevice, width: Int, height: Int) throws -> MTLTexture {
        guard let layout = PackedRGBA8Layout(width: width, height: height),
              rowBytes >= layout.rowBytes,
              height <= length / rowBytes,
              rowBytes % device.minimumLinearTextureAlignment(for: .rgba8Unorm) == 0
        else { throw NativePreviewError.invalidDimensions }
        let fd = name.withCString { openNativeSharedMemory($0, O_RDWR) }
        guard fd >= 0 else { throw NativePreviewError.missingData }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_size >= length else {
            throw NativePreviewError.invalidDimensions
        }
        let mapping = mmap(nil, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)
        guard let mapping, mapping != MAP_FAILED else {
            throw NativePreviewError.missingData
        }
        guard let buffer = device.makeBuffer(bytesNoCopy: mapping, length: length,
            options: .storageModeShared, deallocator: { pointer, size in
                munmap(pointer, size)
            }) else {
            munmap(mapping, length)
            throw NativePreviewError.textureAllocation
        }
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm, width: width, height: height, mipmapped: false)
        descriptor.usage = .shaderRead
        descriptor.storageMode = .shared
        guard let texture = buffer.makeTexture(descriptor: descriptor,
            offset: 0, bytesPerRow: rowBytes) else {
            throw NativePreviewError.textureAllocation
        }
        // The texture retains its backing buffer, whose deallocator owns mmap.
        return texture
    }
}

struct NativePreviewTimings {
    let fetchMs: Double
    let decodeMs: Double
    let uploadMs: Double
    let gpuMs: Double
    let totalMs: Double
    var textureCacheHit: Bool = false
    var sharedMemory: Bool = false

    var payload: [String: Any] {
        [
            "fetchMs": fetchMs,
            "decodeMs": decodeMs,
            "uploadMs": uploadMs,
            "gpuMs": gpuMs,
            "totalMs": totalMs,
            "textureCacheHit": textureCacheHit,
            "sharedMemory": sharedMemory,
        ]
    }
}

// Exact cost bounds and deterministic LRU eviction. Values remain immutable:
// a cached texture must never be recycled as a mutable upload buffer.
final class ByteBudgetCache<Value> {
    private let lock = NSLock()
    private var entries: [String: (value: Value, cost: Int)] = [:]
    private var order: [String] = []
    private var bytes = 0
    let budget: Int
    let limit: Int
    init(budget: Int, limit: Int = 12) { self.budget = budget; self.limit = limit }
    var cost: Int { lock.lock(); defer { lock.unlock() }; return bytes }
    func value(for key: String) -> Value? {
        lock.lock(); defer { lock.unlock() }
        guard let entry = entries[key] else { return nil }
        order.removeAll { $0 == key }; order.append(key)
        return entry.value
    }
    func insert(_ value: Value, key: String, cost: Int) {
        lock.lock(); defer { lock.unlock() }
        if let old = entries.removeValue(forKey: key) { bytes -= old.cost }
        order.removeAll { $0 == key }
        guard cost > 0, cost <= budget, limit > 0 else { return }
        while bytes > budget - cost || entries.count >= limit {
            guard let oldest = order.first else { break }
            order.removeFirst()
            if let old = entries.removeValue(forKey: oldest) { bytes -= old.cost }
        }
        entries[key] = (value, cost); order.append(key); bytes += cost
    }
    func removeAll() {
        lock.lock(); defer { lock.unlock() }
        entries.removeAll(); order.removeAll(); bytes = 0
    }
}

final class PassthroughMetalView: MTKView {
    private var encodeFrame: (() -> Void)?

    func drawFrame(_ encode: @escaping () -> Void) {
        encodeFrame = encode
        defer { encodeFrame = nil }
        // MTKView retires currentDrawable only after its drawing callback
        // returns. Every submission must pass through this lifecycle.
        draw()
    }

    override func draw(_ dirtyRect: NSRect) {
        encodeFrame?()
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

private struct GradeUniforms {
    var tone0 = SIMD4<Float>(repeating: 0)
    var tone1 = SIMD4<Float>(repeating: 0)
    var tone2 = SIMD4<Float>(repeating: 0)
    var tone3 = SIMD4<Float>(repeating: 0)
    var vignetteShape = SIMD4<Float>(0.5, 1, 0, 0)
    var detail0 = SIMD4<Float>(repeating: 0)
    var detail1 = SIMD4<Float>(repeating: 0)
    var curveOn = SIMD4<Float>(repeating: 0)
    var viewport = SIMD4<Float>(1, 1, 0, 0)
    var sourceRegion = SIMD4<Float>(1, 1, 0, 0)
    var compare = SIMD4<Float>(repeating: 0)
    var reference0 = SIMD4<Float>(0, 0, 0.5, 1)
    var reference1 = SIMD4<Float>(repeating: 0)
    var hsl0 = SIMD4<Float>(repeating: 0)
    var hsl1 = SIMD4<Float>(repeating: 0)
    var hsl2 = SIMD4<Float>(repeating: 0)
    var hsl3 = SIMD4<Float>(repeating: 0)
    var hsl4 = SIMD4<Float>(repeating: 0)
    var hsl5 = SIMD4<Float>(repeating: 0)
    var hsl6 = SIMD4<Float>(repeating: 0)
    var hsl7 = SIMD4<Float>(repeating: 0)
    var point0 = SIMD4<Float>(repeating: 0)
    var point1 = SIMD4<Float>(repeating: 0)
    var point2 = SIMD4<Float>(repeating: 0)
    var point3 = SIMD4<Float>(repeating: 0)
    var point4 = SIMD4<Float>(repeating: 0)
    var point5 = SIMD4<Float>(repeating: 0)
    var point6 = SIMD4<Float>(repeating: 0)
    var point7 = SIMD4<Float>(repeating: 0)
    var pointLuma0 = SIMD4<Float>(repeating: 0)
    var pointLuma1 = SIMD4<Float>(repeating: 0)
    var pointUniform0 = SIMD4<Float>(repeating: 0)
    var pointUniform1 = SIMD4<Float>(repeating: 0)
    var pointUniform2 = SIMD4<Float>(repeating: 0)
    var pointUniform3 = SIMD4<Float>(repeating: 0)
    var pointUniform4 = SIMD4<Float>(repeating: 0)
    var pointUniform5 = SIMD4<Float>(repeating: 0)
    var pointUniform6 = SIMD4<Float>(repeating: 0)
    var pointUniform7 = SIMD4<Float>(repeating: 0)
    var pointRefLuma0 = SIMD4<Float>(repeating: 0)
    var pointRefLuma1 = SIMD4<Float>(repeating: 0)
    var pointRefOn0 = SIMD4<Float>(repeating: 0)
    var pointRefOn1 = SIMD4<Float>(repeating: 0)
    var pointCount = SIMD4<Float>(repeating: 0)
    var colorGrade0 = SIMD4<Float>(repeating: 0)
    var colorGrade1 = SIMD4<Float>(repeating: 0)
    var colorGrade2 = SIMD4<Float>(repeating: 0)
    var colorGrade3 = SIMD4<Float>(repeating: 0)
    var colorGradeSettings = SIMD4<Float>(0, 0.5, 0, 0)
    var softProof = SIMD4<Float>(repeating: 0)
    var spotVisualization = SIMD4<Float>(repeating: 0)
    var optics0 = SIMD4<Float>(repeating: 0)
    var optics1 = SIMD4<Float>(1, 0, 0, 0)
    var optics2 = SIMD4<Float>(repeating: 0)
}

private struct LocalUniform {
    var tone = SIMD4<Float>(repeating: 0)
    var color = SIMD4<Float>(repeating: 0)
    var range = SIMD4<Float>(0, 1, 0, 0)
    var detail = SIMD4<Float>(repeating: 0)
    var colorRange = SIMD4<Float>(0, 30, 1, 0)
}

private struct HealUniform {
    var points = SIMD4<Float>(repeating: 0) // target x/y, source x/y
    var settings = SIMD4<Float>(repeating: 0) // radius, feather, opacity, mode
}

struct PackedRGBA8Layout {
    let rowBytes: Int
    let byteCount: Int

    init?(width: Int, height: Int) {
        guard width > 0, height > 0 else { return nil }
        let (rowBytes, rowOverflow) = width.multipliedReportingOverflow(by: 4)
        let (byteCount, countOverflow) = rowBytes.multipliedReportingOverflow(
            by: height)
        guard !rowOverflow, !countOverflow else { return nil }
        self.rowBytes = rowBytes
        self.byteCount = byteCount
    }
}

struct RawSurfaceTextureDescriptor {
    let pixelFormat: MTLPixelFormat
    let textureType: MTLTextureType
    let width: Int
    let height: Int
    let depth: Int
    let mipmapLevelCount: Int
    let sampleCount: Int
    let arrayLength: Int
    let storageMode: MTLStorageMode
    let usage: MTLTextureUsage

    init(texture: MTLTexture) {
        pixelFormat = texture.pixelFormat
        textureType = texture.textureType
        width = texture.width
        height = texture.height
        depth = texture.depth
        mipmapLevelCount = texture.mipmapLevelCount
        sampleCount = texture.sampleCount
        arrayLength = texture.arrayLength
        storageMode = texture.storageMode
        usage = texture.usage
    }

    init(
        pixelFormat: MTLPixelFormat,
        textureType: MTLTextureType = .type2D,
        width: Int,
        height: Int,
        depth: Int = 1,
        mipmapLevelCount: Int = 1,
        sampleCount: Int = 1,
        arrayLength: Int = 1,
        storageMode: MTLStorageMode = .shared,
        usage: MTLTextureUsage = .shaderRead
    ) {
        self.pixelFormat = pixelFormat
        self.textureType = textureType
        self.width = width
        self.height = height
        self.depth = depth
        self.mipmapLevelCount = mipmapLevelCount
        self.sampleCount = sampleCount
        self.arrayLength = arrayLength
        self.storageMode = storageMode
        self.usage = usage
    }

    func isReusableRawSurface(width expectedWidth: Int,
                              height expectedHeight: Int) -> Bool {
        pixelFormat == .rgba8Unorm && textureType == .type2D
            && width == expectedWidth && height == expectedHeight && depth == 1
            && mipmapLevelCount == 1 && sampleCount == 1 && arrayLength == 1
            && storageMode == .shared && usage.contains(.shaderRead)
    }
}

final class NativePreviewRenderer {
    let view: PassthroughMetalView

    private let device: MTLDevice
    private let commandQueue: MTLCommandQueue
    private let pipeline: MTLRenderPipelineState
    private let sampler: MTLSamplerState
    private let textureLoader: MTKTextureLoader
    private var imageTexture: MTLTexture?
    private var fullFrameTexture: MTLTexture?
    private var imageRegion = SIMD4<Float>(1, 1, 0, 0)
    private let textureCache = ByteBudgetCache<MTLTexture>(budget: 256 * 1024 * 1024)
    private var preloadTasks: [URL: URLSessionDataTask] = [:]
    private var preloadEpoch = 0
    private var memoryPressure: DispatchSourceMemoryPressure?
    private var originalTexture: MTLTexture?
    private var referenceTexture: MTLTexture?
    private var curveTexture: MTLTexture
    private var maskTexture: MTLTexture
    private var maskTexturePixels = [UInt8](repeating: 0, count: 4)
    private var grade: [String: Any] = [:]
    private var softProof: [String: Any] = [:]
    private var spotVisualization = SIMD4<Float>(repeating: 0)
    private var localMasks: [[String: Any]] = []
    private var optics: [String: Any] = [:]
    private var heals: [[String: Any]] = []
    private var viewport = SIMD4<Float>(1, 1, 0, 0)
    private var comparePosition: Float = 0
    private var reference: [String: Any] = [:]
    private var loadTask: URLSessionDataTask?
    private var originalLoadTask: URLSessionDataTask?
    private var originalURL: URL?
    private var requestedGeneration = 0
    private var awaitingPhoto = false
    private var preparingSurface = false

    func beginNavigation(generation: Int) {
        requestedGeneration = generation
        awaitingPhoto = true
        fullFrameTexture = nil
        pendingInteraction = nil
        loadTask?.cancel()
        originalLoadTask?.cancel()
    }
    private var requestedOriginalGeneration = 0
    private var renderScheduled = false
    var onInteractionPresented: (([String: Any]) -> Void)?
    private var pendingInteraction: [String: Any]?

    func recordInteraction(_ value: [String: Any]?) {
        if let value { pendingInteraction = value }
    }

    init?() {
        guard let device = MTLCreateSystemDefaultDevice(),
              let queue = device.makeCommandQueue(),
              let libraryURL = Bundle.main.url(
                forResource: "NativePreview", withExtension: "metal"),
              let librarySource = try? String(
                contentsOf: libraryURL, encoding: .utf8),
              let library = try? device.makeLibrary(
                source: librarySource, options: nil),
              let vertex = library.makeFunction(name: "nativePreviewVertex"),
              let fragment = library.makeFunction(name: "nativePreviewFragment")
        else { return nil }

        let metalView = PassthroughMetalView(frame: .zero, device: device)
        metalView.colorPixelFormat = .bgra8Unorm
        metalView.framebufferOnly = true
        metalView.isPaused = true
        metalView.enableSetNeedsDisplay = false
        metalView.autoResizeDrawable = true
        metalView.presentsWithTransaction = true
        metalView.clearColor = MTLClearColorMake(0.0627, 0.0627, 0.0627, 1)
        metalView.colorspace = CGColorSpace(name: CGColorSpace.sRGB)
        metalView.isHidden = true

        let pipelineDescriptor = MTLRenderPipelineDescriptor()
        pipelineDescriptor.vertexFunction = vertex
        pipelineDescriptor.fragmentFunction = fragment
        pipelineDescriptor.colorAttachments[0].pixelFormat = metalView.colorPixelFormat
        guard let pipeline = try? device.makeRenderPipelineState(
            descriptor: pipelineDescriptor) else { return nil }

        let samplerDescriptor = MTLSamplerDescriptor()
        samplerDescriptor.minFilter = .linear
        samplerDescriptor.magFilter = .linear
        samplerDescriptor.sAddressMode = .clampToEdge
        samplerDescriptor.tAddressMode = .clampToEdge
        guard let sampler = device.makeSamplerState(descriptor: samplerDescriptor) else {
            return nil
        }

        let curveDescriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm, width: 256, height: 1, mipmapped: false)
        curveDescriptor.usage = .shaderRead
        curveDescriptor.storageMode = .shared
        let maskDescriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm, width: 1, height: 1, mipmapped: false)
        maskDescriptor.usage = .shaderRead
        maskDescriptor.storageMode = .shared
        guard let curveTexture = device.makeTexture(descriptor: curveDescriptor),
              let maskTexture = device.makeTexture(descriptor: maskDescriptor) else {
            return nil
        }

        self.device = device
        commandQueue = queue
        self.pipeline = pipeline
        self.sampler = sampler
        view = metalView
        textureLoader = MTKTextureLoader(device: device)
        self.curveTexture = curveTexture
        self.maskTexture = maskTexture
        let pressure = DispatchSource.makeMemoryPressureSource(
            eventMask: [.warning, .critical], queue: .main)
        pressure.setEventHandler { [weak self] in
            self?.textureCache.removeAll()
            self?.cancelPreloads()
        }
        pressure.resume()
        memoryPressure = pressure
        updateCurveTexture()
    }

    deinit { memoryPressure?.cancel() }

    func setFrame(
        _ frame: NSRect,
        uvScale: SIMD2<Float> = SIMD2<Float>(1, 1),
        uvOffset: SIMD2<Float> = SIMD2<Float>(0, 0),
        visible: Bool
    ) {
        guard !awaitingPhoto else { return }
        let nextFrame = frame.integral
        let nextViewport = SIMD4<Float>(uvScale.x, uvScale.y, uvOffset.x, uvOffset.y)
        let hidden = !visible || frame.width < 1 || frame.height < 1
        guard view.frame != nextFrame || viewport != nextViewport || view.isHidden != hidden else { return }
        // Resize and the matching source window must reach the compositor in
        // the same transaction. Otherwise it stretches the previous drawable
        // into the new clipped rectangle until the deferred render arrives.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        if view.frame != nextFrame { view.frame = nextFrame }
        viewport = nextViewport
        view.isHidden = hidden
        if !hidden, imageTexture != nil { render() }
    }

    func setBackgroundColor(_ color: NSColor) {
        guard let converted = color.usingColorSpace(.sRGB) else { return }
        view.clearColor = MTLClearColorMake(
            Double(converted.redComponent), Double(converted.greenComponent),
            Double(converted.blueComponent), 1)
        render()
    }

    func hide() {
        loadTask?.cancel()
        originalLoadTask?.cancel()
        view.isHidden = true
        cancelPreloads()
    }

    func updateGrade(
        _ value: [String: Any],
        softProof: [String: Any] = [:],
        updateCurves: Bool = true,
        renderNow: Bool = true
    ) {
        var next = value
        if !updateCurves {
            for key in ["curveL", "curveR", "curveG", "curveB"] {
                if let curve = grade[key] { next[key] = curve }
            }
        }
        grade = next
        self.softProof = softProof
        if updateCurves { updateCurveTexture() }
        if renderNow { scheduleRender() }
    }

    func updateMasks(_ payload: [String: Any]) {
        localMasks = payload["masks"] as? [[String: Any]] ?? []
        guard let width = payload["width"] as? Int,
              let height = payload["height"] as? Int,
              width > 0, height > 0, width <= 1024, height <= 4096,
              let encoded = payload["data"] as? String,
              let data = Data(base64Encoded: encoded)
        else {
            scheduleRender()
            return
        }
        if let channel = payload["channel"] as? Int,
           let tile = payload["tile"] as? Int,
           (0..<4).contains(channel), (0..<4).contains(tile),
           data.count == width * height,
           maskTexture.width > 1, maskTexture.height > 1 {
            let targetWidth = maskTexture.width
            let tileCount = max(1, min(4, (localMasks.count + 3) / 4))
            let targetHeight = maskTexture.height / tileCount
            guard tile < tileCount, targetHeight > 0 else {
                scheduleRender()
                return
            }
            guard let atlasLayout = PackedRGBA8Layout(
                width: targetWidth, height: maskTexture.height) else {
                scheduleRender()
                return
            }
            if maskTexturePixels.count != atlasLayout.byteCount {
                maskTexturePixels = [UInt8](
                    repeating: 0, count: atlasLayout.byteCount)
            }
            data.withUnsafeBytes { source in
                guard let bytes = source.bindMemory(to: UInt8.self).baseAddress else {
                    return
                }
                for y in 0..<targetHeight {
                    let sourceY = min(height - 1, y * height / targetHeight)
                    for x in 0..<targetWidth {
                        let sourceX = min(width - 1, x * width / targetWidth)
                        let targetY = tile * targetHeight + y
                        maskTexturePixels[(targetY * targetWidth + x) * 4 + channel] =
                            bytes[sourceY * width + sourceX]
                    }
                }
            }
            maskTexturePixels.withUnsafeBytes { bytes in
                guard let base = bytes.baseAddress else { return }
                let tileY = tile * targetHeight
                let tileByteOffset = tileY * atlasLayout.rowBytes
                maskTexture.replace(
                    region: MTLRegionMake2D(
                        0, tileY, targetWidth, targetHeight),
                    mipmapLevel: 0,
                    withBytes: base.advanced(by: tileByteOffset),
                    bytesPerRow: atlasLayout.rowBytes)
            }
            scheduleRender()
            return
        }
        guard let maskLayout = PackedRGBA8Layout(width: width, height: height),
              data.count == maskLayout.byteCount else {
            scheduleRender()
            return
        }
        if maskTexture.width != width || maskTexture.height != height {
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .rgba8Unorm, width: width, height: height,
                mipmapped: false)
            descriptor.usage = .shaderRead
            descriptor.storageMode = .shared
            guard let replacement = device.makeTexture(descriptor: descriptor) else {
                return
            }
            maskTexture = replacement
        }
        maskTexturePixels = Array(data)
        data.withUnsafeBytes { bytes in
            guard let base = bytes.baseAddress else { return }
            maskTexture.replace(
                region: MTLRegionMake2D(0, 0, width, height),
                mipmapLevel: 0, withBytes: base,
                bytesPerRow: maskLayout.rowBytes)
        }
        scheduleRender()
    }

    func updateSpotVisualization(_ payload: [String: Any]) {
        let threshold = (payload["threshold"] as? NSNumber)?.doubleValue ?? 0
        spotVisualization = SIMD4<Float>(
            (payload["enabled"] as? Bool ?? false) ? 1 : 0,
            Float(threshold.isFinite ? max(0, min(1, threshold)) : 0), 0, 0)
        scheduleRender()
    }

    func updateEdits(optics: [String: Any], heals: [[String: Any]]) {
        self.optics = optics
        self.heals = Array(heals.prefix(16))
        scheduleRender()
    }

    func updateComparePosition(_ value: Double) {
        comparePosition = Float(max(0, min(1, value)))
        scheduleRender()
    }

    func updateReference(_ payload: [String: Any]) {
        reference = payload
        if payload["clear"] as? Bool ?? false {
            referenceTexture = nil
            scheduleRender()
            return
        }
        if let encoded = payload["data"] as? String,
           let data = Data(base64Encoded: encoded),
           let texture = try? textureLoader.newTexture(
               data: data,
               options: [
                   .SRGB: false,
                   .textureUsage: NSNumber(
                       value: MTLTextureUsage.shaderRead.rawValue),
                   .textureStorageMode: NSNumber(
                       value: MTLStorageMode.shared.rawValue),
               ]) {
            referenceTexture = texture
        }
        scheduleRender()
    }

    func loadOriginal(_ surface: NativeSurfaceDescription, generation: Int) {
        requestedOriginalGeneration = generation
        if originalURL == surface.url, originalTexture != nil {
            scheduleRender()
            return
        }
        originalLoadTask?.cancel()
        originalURL = surface.url
        originalTexture = nil
        var request = URLRequest(url: surface.url)
        request.cachePolicy = .returnCacheDataElseLoad
        let task = URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let self, error == nil, let data else { return }
            do {
                let texture = try self.textureLoader.newTexture(
                    data: data,
                    options: [
                        .SRGB: false,
                        .textureUsage: NSNumber(value: MTLTextureUsage.shaderRead.rawValue),
                        .textureStorageMode: NSNumber(value: MTLStorageMode.shared.rawValue),
                    ])
                DispatchQueue.main.async {
                    guard generation == self.requestedOriginalGeneration,
                          surface.url == self.originalURL else { return }
                    self.originalTexture = texture
                    self.render()
                }
            } catch {
                return
            }
        }
        originalLoadTask = task
        task.resume()
    }

    func clearOriginal() {
        originalLoadTask?.cancel()
        originalURL = nil
        originalTexture = nil
        scheduleRender()
    }

    private func cacheKey(_ surface: NativeSurfaceDescription) -> String {
        "\(surface.url.absoluteString)|\(surface.format)|\(surface.width)x\(surface.height)|\(surface.rowBytes)"
    }

    private func cacheTexture(_ texture: MTLTexture, surface: NativeSurfaceDescription) {
        guard let layout = PackedRGBA8Layout(width: texture.width, height: texture.height) else { return }
        textureCache.insert(texture, key: cacheKey(surface),
            cost: texture.buffer?.length ?? layout.byteCount)
    }

    func cancelPreloads(epoch: Int? = nil) {
        preloadEpoch = epoch ?? (preloadEpoch + 1)
        preloadTasks.values.forEach { $0.cancel() }
        preloadTasks.removeAll()
    }

    func preload(_ surface: NativeSurfaceDescription, epoch: Int) {
        guard epoch == preloadEpoch, preloadTasks.count < 3,
              preloadTasks[surface.url] == nil,
              textureCache.value(for: cacheKey(surface)) == nil else { return }
        var request = URLRequest(url: surface.url)
        request.cachePolicy = .returnCacheDataElseLoad
        let task = URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let self else { return }
            DispatchQueue.main.async {
                guard epoch == self.preloadEpoch else { return }
                self.preloadTasks.removeValue(forKey: surface.url)
                guard error == nil, let data else { return }
                // Decode/upload off the UI thread, then publish an immutable texture.
                DispatchQueue.global(qos: .utility).async {
                    guard let texture = try? self.decodeSurface(data, surface: surface) else { return }
                    DispatchQueue.main.async {
                        guard epoch == self.preloadEpoch else { return }
                        self.cacheTexture(texture, surface: surface)
                    }
                }
            }
        }
        task.priority = URLSessionTask.lowPriority
        preloadTasks[surface.url] = task
        task.resume()
    }

    private func decodeSurface(_ data: Data, surface: NativeSurfaceDescription) throws -> MTLTexture {
        if surface.format == "rgba8" {
            let layout = try validateRawSurface(data, expected: surface)
            return try uploadRawSurface(data, offset: layout.headerBytes,
                width: layout.width, height: layout.height, rowBytes: layout.rowBytes)
        }
        return try textureLoader.newTexture(data: data, options: [
            .SRGB: false,
            .textureUsage: NSNumber(value: MTLTextureUsage.shaderRead.rawValue),
            .textureStorageMode: NSNumber(value: MTLStorageMode.shared.rawValue),
        ])
    }

    func load(
        _ surface: NativeSurfaceDescription,
        generation: Int,
        grade: [String: Any],
        prepare: (() -> Void)? = nil,
        completion: @escaping (Result<NativePreviewTimings, Error>) -> Void
    ) {
        loadTask?.cancel()
        requestedGeneration = generation
        let started = ProcessInfo.processInfo.systemUptime
        if let cached = textureCache.value(for: cacheKey(surface)) {
            installSurface(cached, surface: surface, grade: grade, prepare: prepare) { gpuMs in
                guard let gpuMs else {
                    completion(.failure(NativePreviewError.drawableUnavailable)); return
                }
                completion(.success(NativePreviewTimings(fetchMs: 0, decodeMs: 0,
                    uploadMs: 0, gpuMs: gpuMs,
                    totalMs: (ProcessInfo.processInfo.systemUptime - started) * 1000,
                    textureCacheHit: true)))
            }
            return
        }
        let fetchHTTP = { [weak self] in
            guard let self, generation == self.requestedGeneration else { return }
            var request = URLRequest(url: surface.url)
            request.cachePolicy = .returnCacheDataElseLoad
            let task = URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
                guard let self else { return }
                if let error {
                    if (error as NSError).code != NSURLErrorCancelled {
                        DispatchQueue.main.async { completion(.failure(error)) }
                    }
                    return
                }
                guard let data else {
                    DispatchQueue.main.async {
                        completion(.failure(NativePreviewError.missingData))
                    }
                    return
                }
                let fetched = ProcessInfo.processInfo.systemUptime
                do {
                    let decodedStarted = ProcessInfo.processInfo.systemUptime
                    let texture: MTLTexture
                    let decodedAt: Double
                    let uploadedAt: Double
                    if surface.format == "rgba8" {
                        let validated = try self.validateRawSurface(data, expected: surface)
                        decodedAt = ProcessInfo.processInfo.systemUptime
                        texture = try self.uploadRawSurface(
                            data, offset: validated.headerBytes,
                            width: validated.width, height: validated.height,
                            rowBytes: validated.rowBytes)
                        uploadedAt = ProcessInfo.processInfo.systemUptime
                    } else {
                        texture = try self.textureLoader.newTexture(
                            data: data,
                            options: [
                                .SRGB: false,
                                .textureUsage: NSNumber(value: MTLTextureUsage.shaderRead.rawValue),
                                .textureStorageMode: NSNumber(value: MTLStorageMode.shared.rawValue),
                            ])
                        decodedAt = ProcessInfo.processInfo.systemUptime
                        uploadedAt = decodedAt
                    }
                    DispatchQueue.main.async {
                        guard generation == self.requestedGeneration else { return }
                        self.cacheTexture(texture, surface: surface)
                        self.installSurface(texture, surface: surface, grade: grade, prepare: prepare) { gpuMs in
                            guard let gpuMs else {
                                completion(.failure(
                                    NativePreviewError.drawableUnavailable))
                                return
                            }
                            let finished = ProcessInfo.processInfo.systemUptime
                            completion(.success(NativePreviewTimings(
                                fetchMs: (fetched - started) * 1000,
                                decodeMs: (decodedAt - decodedStarted) * 1000,
                                uploadMs: (uploadedAt - decodedAt) * 1000,
                                gpuMs: gpuMs,
                                totalMs: (finished - started) * 1000)))
                        }
                    }
                } catch {
                    DispatchQueue.main.async { completion(.failure(error)) }
                }
            }
            self.loadTask = task
            task.resume()
        }
        if let shared = surface.sharedMemory, surface.format == "rgba8" {
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                guard let texture = try? shared.texture(device: self.device,
                    width: surface.width, height: surface.height) else {
                    DispatchQueue.main.async(execute: fetchHTTP)
                    return
                }
                let mappedAt = ProcessInfo.processInfo.systemUptime
                DispatchQueue.main.async {
                    guard generation == self.requestedGeneration else { return }
                    self.cacheTexture(texture, surface: surface)
                    self.installSurface(texture, surface: surface, grade: grade, prepare: prepare) { gpuMs in
                        guard let gpuMs else {
                            completion(.failure(NativePreviewError.drawableUnavailable))
                            return
                        }
                        completion(.success(NativePreviewTimings(
                            fetchMs: (mappedAt - started) * 1000,
                            decodeMs: 0, uploadMs: 0, gpuMs: gpuMs,
                            totalMs: (ProcessInfo.processInfo.systemUptime - started) * 1000,
                            sharedMemory: true)))
                    }
                }
            }
        } else {
            fetchHTTP()
        }
    }

    private func installSurface(
        _ texture: MTLTexture, surface: NativeSurfaceDescription,
        grade: [String: Any], prepare: (() -> Void)?,
        completion: @escaping (Double?) -> Void
    ) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        awaitingPhoto = false
        // Preparation can resize the view and update masks. Suppress any
        // intermediate draw until the new pixels and recipe are all installed.
        preparingSurface = true
        prepare?()
        updateGrade(grade, renderNow: false)
        setImageTexture(texture, surface: surface)
        preparingSurface = false
        render(completion: completion)
    }

    private func setImageTexture(_ texture: MTLTexture, surface: NativeSurfaceDescription) {
        imageTexture = texture
        imageRegion = surface.imageRegion
        if imageRegion == SIMD4<Float>(1, 1, 0, 0) { fullFrameTexture = texture }
    }

    private func validateRawSurface(
        _ data: Data,
        expected: NativeSurfaceDescription
    ) throws -> (width: Int, height: Int, rowBytes: Int, headerBytes: Int) {
        guard data.count >= 16,
              data.prefix(4).elementsEqual([0x46, 0x4c, 0x52, 0x41])
        else { throw NativePreviewError.invalidHeader }
        let width = Int(data.littleEndianUInt32(at: 4))
        let height = Int(data.littleEndianUInt32(at: 8))
        let rowBytes = Int(data.littleEndianUInt32(at: 12))
        let headerBytes = max(16, expected.headerBytes)
        guard let layout = PackedRGBA8Layout(width: width, height: height),
              width == expected.width, height == expected.height,
              rowBytes == layout.rowBytes,
              headerBytes <= data.count,
              layout.byteCount == data.count - headerBytes
        else { throw NativePreviewError.invalidDimensions }
        return (width, height, rowBytes, headerBytes)
    }

    private func uploadRawSurface(
        _ data: Data,
        offset: Int,
        width: Int,
        height: Int,
        rowBytes: Int
    ) throws -> MTLTexture {
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm, width: width, height: height, mipmapped: false)
        descriptor.usage = .shaderRead
        descriptor.storageMode = .shared
        guard let texture = device.makeTexture(descriptor: descriptor) else {
            throw NativePreviewError.textureAllocation
        }
        data.withUnsafeBytes { bytes in
            guard let base = bytes.baseAddress else { return }
            texture.replace(
                region: MTLRegionMake2D(0, 0, width, height),
                mipmapLevel: 0,
                withBytes: base.advanced(by: offset),
                bytesPerRow: rowBytes)
        }
        return texture
    }

    private func scheduleRender() {
        guard !renderScheduled else { return }
        renderScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self, self.renderScheduled else { return }
            self.renderScheduled = false
            self.render()
        }
    }

    private func updateCurveTexture() {
        let keys = ["curveL", "curveR", "curveG", "curveB"]
        var bytes = [UInt8](repeating: 0, count: 256 * 4)
        for index in 0..<256 {
            for channel in 0..<4 {
                let curve = grade[keys[channel]] as? [Any]
                let value = curve.flatMap { index < $0.count ? number($0[index]) : nil }
                    ?? Double(index) / 255.0
                bytes[index * 4 + channel] = UInt8(
                    max(0, min(255, (value * 255).rounded(.toNearestOrEven))))
            }
        }
        bytes.withUnsafeBytes { pointer in
            curveTexture.replace(
                region: MTLRegionMake2D(0, 0, 256, 1),
                mipmapLevel: 0,
                withBytes: pointer.baseAddress!,
                bytesPerRow: 256 * 4)
        }
    }

    private func uniforms() -> GradeUniforms {
        func value(_ key: String, _ fallback: Double = 0) -> Float {
            Float(number(grade[key]) ?? fallback)
        }
        func hsl(_ name: String) -> SIMD4<Float> {
            let bands = grade["hsl"] as? [String: Any]
            let entry = bands?[name] as? [String: Any]
            return SIMD4<Float>(
                Float(number(entry?["h"]) ?? 0),
                Float(number(entry?["s"]) ?? 0),
                Float(number(entry?["l"]) ?? 0),
                0)
        }
        var output = GradeUniforms()
        output.tone0 = SIMD4<Float>(
            value("exposure"), value("contrast"),
            value("highlights"), value("shadows"))
        output.tone1 = SIMD4<Float>(
            value("whites"), value("blacks"), value("temp"), value("tint"))
        output.tone2 = SIMD4<Float>(
            value("vibrance"), value("saturation"),
            value("texture"), value("clarity"))
        let width = max(1, imageTexture?.width ?? 1)
        let height = max(1, imageTexture?.height ?? 1)
        output.tone3 = SIMD4<Float>(
            value("dehaze"), value("vignette"),
            imageRegion.x / Float(width), imageRegion.y / Float(height))
        output.vignetteShape = SIMD4<Float>(
            value("vignetteSize", 0.5), value("vignetteFeather", 1), 0, 0)
        output.detail0 = SIMD4<Float>(
            value("sharpness"), value("sharpenRadius", 1),
            value("sharpenDetail", 0.25), value("sharpenMasking"))
        output.detail1 = SIMD4<Float>(
            value("luminanceNoise"), value("colorNoise"),
            value("chromaticAberrationRedCyan"),
            value("chromaticAberrationBlueYellow"))
        output.curveOn = SIMD4<Float>(
            grade["curveL"] == nil ? 0 : 1,
            grade["curveR"] == nil ? 0 : 1,
            grade["curveG"] == nil ? 0 : 1,
            grade["curveB"] == nil ? 0 : 1)
        output.viewport = viewport
        output.sourceRegion = imageRegion
        output.compare = SIMD4<Float>(
            originalTexture == nil ? 0 : comparePosition, 0, 0, 0)
        let referenceActive = reference["active"] as? Bool ?? false
        output.reference0 = SIMD4<Float>(
            referenceActive && referenceTexture != nil ? 1 : 0,
            reference["mode"] as? String == "split" ? 1 : 0,
            Float(number(reference["amount"]) ?? 0.5),
            Float(number(reference["scale"]) ?? 1))
        output.reference1 = SIMD4<Float>(
            Float(number(reference["x"]) ?? 0),
            Float(number(reference["y"]) ?? 0), 0, 0)
        output.hsl0 = hsl("red")
        output.hsl1 = hsl("orange")
        output.hsl2 = hsl("yellow")
        output.hsl3 = hsl("green")
        output.hsl4 = hsl("aqua")
        output.hsl5 = hsl("blue")
        output.hsl6 = hsl("purple")
        output.hsl7 = hsl("magenta")
        let points = grade["pointColor"] as? [[String: Any]] ?? []
        func point(_ index: Int) -> SIMD4<Float> {
            guard index < points.count else {
                return SIMD4<Float>(0, 30, 0, 0)
            }
            let item = points[index]
            return SIMD4<Float>(
                Float(number(item["hue"]) ?? 0),
                Float(number(item["range"]) ?? 30),
                Float(number(item["hueShift"]) ?? 0),
                Float(number(item["saturation"]) ?? 0))
        }
        output.point0 = point(0); output.point1 = point(1)
        output.point2 = point(2); output.point3 = point(3)
        output.point4 = point(4); output.point5 = point(5)
        output.point6 = point(6); output.point7 = point(7)
        func pointLuma(_ index: Int) -> Float {
            guard index < points.count else { return 0 }
            return Float(number(points[index]["luminance"]) ?? 0)
        }
        output.pointLuma0 = SIMD4<Float>(
            pointLuma(0), pointLuma(1), pointLuma(2), pointLuma(3))
        output.pointLuma1 = SIMD4<Float>(
            pointLuma(4), pointLuma(5), pointLuma(6), pointLuma(7))
        func pointUniform(_ index: Int) -> SIMD4<Float> {
            guard index < points.count else { return .zero }
            let item = points[index]
            return SIMD4<Float>(
                Float(number(item["uniformHue"]) ?? 0),
                Float(number(item["uniformSaturation"]) ?? 0),
                Float(number(item["uniformLuminance"]) ?? 0),
                Float(number(item["refSaturation"]) ?? 0))
        }
        output.pointUniform0 = pointUniform(0); output.pointUniform1 = pointUniform(1)
        output.pointUniform2 = pointUniform(2); output.pointUniform3 = pointUniform(3)
        output.pointUniform4 = pointUniform(4); output.pointUniform5 = pointUniform(5)
        output.pointUniform6 = pointUniform(6); output.pointUniform7 = pointUniform(7)
        func pointReference(_ index: Int, _ key: String) -> Float {
            guard index < points.count else { return 0 }
            return Float(number(points[index][key]) ?? 0)
        }
        func pointReferenceOn(_ index: Int) -> Float {
            guard index < points.count else { return 0 }
            return points[index]["refSaturation"] != nil &&
                points[index]["refLuminance"] != nil ? 1 : 0
        }
        output.pointRefLuma0 = SIMD4<Float>(
            pointReference(0, "refLuminance"), pointReference(1, "refLuminance"),
            pointReference(2, "refLuminance"), pointReference(3, "refLuminance"))
        output.pointRefLuma1 = SIMD4<Float>(
            pointReference(4, "refLuminance"), pointReference(5, "refLuminance"),
            pointReference(6, "refLuminance"), pointReference(7, "refLuminance"))
        output.pointRefOn0 = SIMD4<Float>(
            pointReferenceOn(0), pointReferenceOn(1),
            pointReferenceOn(2), pointReferenceOn(3))
        output.pointRefOn1 = SIMD4<Float>(
            pointReferenceOn(4), pointReferenceOn(5),
            pointReferenceOn(6), pointReferenceOn(7))
        output.pointCount.x = Float(min(points.count, 8))
        let grading = grade["colorGrading"] as? [String: Any]
        func colorGrade(_ name: String) -> SIMD4<Float> {
            let item = grading?[name] as? [String: Any] ?? [:]
            return SIMD4<Float>(
                Float(number(item["hue"]) ?? 0),
                Float(number(item["saturation"]) ?? 0),
                Float(number(item["luminance"]) ?? 0), 0)
        }
        output.colorGrade0 = colorGrade("shadows")
        output.colorGrade1 = colorGrade("midtones")
        output.colorGrade2 = colorGrade("highlights")
        output.colorGrade3 = colorGrade("global")
        output.colorGradeSettings = SIMD4<Float>(
            Float(number(grading?["balance"]) ?? 0),
            Float(number(grading?["blending"]) ?? 0.5),
            grading == nil ? 0 : 1, 0)
        let proofTargets = ["srgb": 0.0, "display_p3": 1.0,
                            "matte": 2.0, "gloss": 3.0]
        output.softProof = SIMD4<Float>(
            (softProof["enabled"] as? Bool ?? false) ? 1 : 0,
            Float(proofTargets[softProof["profile"] as? String ?? "srgb"] ?? 0),
            (softProof["paper"] as? Bool ?? false) ? 1 : 0,
            (softProof["gamut"] as? Bool ?? false) ? 1 : 0)
        output.spotVisualization = spotVisualization
        func optic(_ key: String, _ fallback: Double = 0) -> Float {
            Float(number(optics[key]) ?? fallback)
        }
        output.optics0 = SIMD4<Float>(
            optic("distortion"), optic("vertical"), optic("horizontal"),
            optic("rotate") * .pi / 180)
        output.optics1 = SIMD4<Float>(
            optic("scale", 1), optic("vignette"), Float(heals.count),
            Float(max(1, min(4, (localMasks.count + 3) / 4))))
        output.optics2 = SIMD4<Float>(
            (optics["flipHorizontal"] as? Bool ?? false) ? 1 : 0,
            (optics["flipVertical"] as? Bool ?? false) ? 1 : 0, 0, 0)
        return output
    }

    private func localUniforms() -> [LocalUniform] {
        (0..<16).map { index in
            guard index < localMasks.count else { return LocalUniform() }
            let mask = localMasks[index]
            let values = mask["grade"] as? [String: Any] ?? [:]
            func value(_ key: String, _ fallback: Double = 0) -> Float {
                Float(number(values[key]) ?? fallback)
            }
            let hue = number(mask["colorHue"])
            return LocalUniform(
                tone: SIMD4<Float>(
                    value("exposure"), value("contrast"),
                    value("highlights"), value("shadows")),
                color: SIMD4<Float>(
                    value("temp"), value("tint"), value("saturation"),
                    Float(number(mask["opacity"]) ?? 1)),
                range: SIMD4<Float>(
                    Float(number(mask["lumaLow"]) ?? 0),
                    Float(number(mask["lumaHigh"]) ?? 1),
                    (mask["enabled"] as? Bool ?? true) ? 1 : 0, 0),
                detail: SIMD4<Float>(value("texture"), value("clarity"), 0, 0),
                colorRange: SIMD4<Float>(
                    Float(hue ?? 0), Float(number(mask["colorRange"]) ?? 30),
                    Float(number(mask["colorAmount"]) ?? 1), hue == nil ? 0 : 1))
        }
    }

    private func healUniforms() -> [HealUniform] {
        heals.map { spot in
            func point(_ key: String) -> SIMD2<Float> {
                guard let values = spot[key] as? [Any], values.count >= 2 else {
                    return SIMD2<Float>(repeating: 0.5)
                }
                return SIMD2<Float>(
                    Float(number(values[0]) ?? 0.5),
                    Float(number(values[1]) ?? 0.5))
            }
            let target = point("target")
            let source = point("source")
            let mode = spot["mode"] as? String ?? "heal"
            let modeValue: Float = mode == "remove" ? 1 : mode == "clone" ? 3 : 2
            return HealUniform(
                points: SIMD4<Float>(target.x, target.y, source.x, source.y),
                settings: SIMD4<Float>(
                    Float(number(spot["radius"]) ?? 0.04),
                    Float(number(spot["feather"]) ?? 0.65),
                    Float(number(spot["opacity"]) ?? 1), modeValue))
        }
    }

    /// Render the exact Metal preview state into a CPU-readable image for the
    /// product journey. This avoids screen-recording permissions and proves
    /// the texture that the MTKView is presenting contains real pixels.
    func snapshot() -> NSImage? {
        guard !view.isHidden, imageTexture != nil else { return nil }
        let scale = max(1, view.window?.backingScaleFactor ?? 1)
        let width = max(1, Int((view.bounds.width * scale).rounded()))
        let height = max(1, Int((view.bounds.height * scale).rounded()))
        let textureDescriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: view.colorPixelFormat,
            width: width,
            height: height,
            mipmapped: false)
        textureDescriptor.usage = [.renderTarget, .shaderRead]
        textureDescriptor.storageMode = .shared
        guard let texture = device.makeTexture(descriptor: textureDescriptor),
              let commandBuffer = commandQueue.makeCommandBuffer()
        else { return nil }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = view.clearColor
        guard let encoder = commandBuffer.makeRenderCommandEncoder(
            descriptor: pass) else { return nil }
        encodePreview(into: encoder)
        encoder.endEncoding()
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()
        guard commandBuffer.status == .completed else { return nil }

        let bytesPerRow = width * 4
        var bytes = [UInt8](repeating: 0, count: bytesPerRow * height)
        texture.getBytes(
            &bytes,
            bytesPerRow: bytesPerRow,
            from: MTLRegionMake2D(0, 0, width, height),
            mipmapLevel: 0)
        let data = Data(bytes)
        guard let provider = CGDataProvider(data: data as CFData),
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let image = CGImage(
                width: width,
                height: height,
                bitsPerComponent: 8,
                bitsPerPixel: 32,
                bytesPerRow: bytesPerRow,
                space: colorSpace,
                bitmapInfo: CGBitmapInfo(
                    rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue)
                    .union(.byteOrder32Little),
                provider: provider,
                decode: nil,
                shouldInterpolate: true,
                intent: .defaultIntent)
        else { return nil }
        return NSImage(cgImage: image, size: view.bounds.size)
    }

    private func encodePreview(into encoder: MTLRenderCommandEncoder) {
        guard let imageTexture else { return }
        var uniforms = uniforms()
        var healValues = healUniforms()
        let localValues = localUniforms()
        if healValues.isEmpty { healValues = [HealUniform()] }
        encoder.setRenderPipelineState(pipeline)
        encoder.setFragmentTexture(imageTexture, index: 0)
        encoder.setFragmentTexture(curveTexture, index: 1)
        encoder.setFragmentTexture(originalTexture ?? imageTexture, index: 2)
        encoder.setFragmentTexture(maskTexture, index: 3)
        encoder.setFragmentTexture(referenceTexture ?? imageTexture, index: 4)
        encoder.setFragmentTexture(fullFrameTexture ?? imageTexture, index: 5)
        encoder.setFragmentSamplerState(sampler, index: 0)
        encoder.setFragmentBytes(
            &uniforms, length: MemoryLayout<GradeUniforms>.stride, index: 0)
        healValues.withUnsafeBytes { bytes in
            encoder.setFragmentBytes(
                bytes.baseAddress!, length: bytes.count, index: 1)
        }
        localValues.withUnsafeBytes { bytes in
            encoder.setFragmentBytes(
                bytes.baseAddress!, length: bytes.count, index: 2)
        }
        encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
    }

    private func render(
        attempt: Int = 0,
        completion: ((Double?) -> Void)? = nil
    ) {
        guard !preparingSurface else { return }
        view.drawFrame { [self] in
            renderFrame(attempt: attempt, completion: completion)
        }
    }

    private func renderFrame(
        attempt: Int,
        completion: ((Double?) -> Void)?
    ) {
        renderScheduled = false
        guard !awaitingPhoto, !view.isHidden, imageTexture != nil,
              let descriptor = view.currentRenderPassDescriptor,
              let drawable = view.currentDrawable,
              let commandBuffer = commandQueue.makeCommandBuffer(),
              let encoder = commandBuffer.makeRenderCommandEncoder(
                descriptor: descriptor)
        else {
            if let completion {
                if attempt < 30 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0 / 60.0) {
                        self.render(attempt: attempt + 1, completion: completion)
                    }
                } else {
                    completion(nil)
                }
            }
            return
        }
        encodePreview(into: encoder)
        encoder.endEncoding()
        if var sample = pendingInteraction {
            pendingInteraction = nil
            let submittedAt = ProcessInfo.processInfo.systemUptime
            let callback = onInteractionPresented
            // Some offscreen/occluded CAMetalLayer configurations do not issue
            // drawable callbacks. Record GPU completion separately; never label
            // it as physical presentation.
            let submittedSample = sample
            commandBuffer.addCompletedHandler { buffer in
                var completed = submittedSample
                completed["measurement"] = "gpu-completed"
                completed["nativeGpuMs"] = max(0, buffer.gpuEndTime - buffer.gpuStartTime) * 1000
                DispatchQueue.main.async { callback?(completed) }
            }
            drawable.addPresentedHandler { shown in
                sample["measurement"] = "drawable-presented"
                sample["nativePresentMs"] = max(0, shown.presentedTime - submittedAt) * 1000
                DispatchQueue.main.async { callback?(sample) }
            }
        }
        if let completion {
            let started = ProcessInfo.processInfo.systemUptime
            commandBuffer.addCompletedHandler { _ in
                completion((ProcessInfo.processInfo.systemUptime - started) * 1000)
            }
        }
        commandBuffer.commit()
        // Transactional CAMetalLayer presentation requires scheduling the
        // commands before presenting directly on the drawable (Apple's
        // presentsWithTransaction contract). Do not use commandBuffer.present.
        commandBuffer.waitUntilScheduled()
        drawable.present()
    }
}

private func number(_ value: Any?) -> Double? {
    if let number = value as? NSNumber { return number.doubleValue }
    if let value = value as? Double { return value }
    if let value = value as? Int { return Double(value) }
    return nil
}

private extension Data {
    func littleEndianUInt32(at offset: Int) -> UInt32 {
        withUnsafeBytes { bytes in
            let start = bytes.baseAddress!.advanced(by: offset)
            return start.loadUnaligned(as: UInt32.self).littleEndian
        }
    }
}

private enum NativePreviewError: LocalizedError {
    case missingData
    case invalidHeader
    case invalidDimensions
    case textureAllocation
    case drawableUnavailable

    var errorDescription: String? {
        switch self {
        case .missingData: return "The native preview response was empty."
        case .invalidHeader: return "The native preview header was invalid."
        case .invalidDimensions: return "The native preview dimensions were invalid."
        case .textureAllocation: return "Metal could not allocate the preview texture."
        case .drawableUnavailable: return "Metal could not acquire a preview drawable."
        }
    }
}
